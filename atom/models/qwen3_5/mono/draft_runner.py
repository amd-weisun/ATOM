# SPDX-License-Identifier: MIT
# Copyright (C) 2026, Advanced Micro Devices, Inc. All rights reserved.
"""The MTP draft's decoder layer on the mono kernel.

Qwen3.5's MTP draft (``qwen3_5_mtp.Qwen3_5MultiTokenPredictor``) is embed, two RMSNorms, a concat
with the target's hidden state, ``fc`` and its all-gather, then ONE full-attention decoder layer,
then the final norm. That layer is the target's full-attention kernel (``runner``) over the draft
layer's own weights and KV cache; everything around it stays ATOM's.

ATOM runs the draft eagerly for Qwen3.5 (its MRoPE positions keep the draft out of a CUDA graph,
``EagleProposer._declare_draft_graphs``), so each draft step launched its layer kernel by kernel;
on mono the layer is one launch. A propose loop's steps come in two shapes: step 0 runs the target's
verify rows (``spec_steps + 1`` consecutive tokens a request, the kernel's verify layout), steps 1..
one row a request. Each shape has its own launch widths (``WidthBuilds``).
"""

import logging

import torch
from aiter.dist.parallel_state import get_tp_group

from atom.models.qwen3_5.mono.config import MAX_TOKENS, Q35Config
from atom.models.qwen3_5.mono.op import Qwen35MonoKernel, Qwen35Variant
from atom.models.qwen3_5.mono.runner import Qwen35MonoDecodeRunner
from atom.models.qwen3_5.mono.weights import LayerWeights, rope_tables
from atom.mono.plan.execution import BLOCKS
from atom.mono.runtime.consensus import MonoUnsupported, bind_agreed
from atom.mono.runtime.widths import WidthBuilds
from atom.utils import envs
from atom.utils.forward_context import get_forward_context

logger = logging.getLogger("atom")


class Qwen35MonoDraftRunner(Qwen35MonoDecodeRunner):
    """The draft (``Qwen3_5MTP``) decoder layer of up to ``MAX_TOKENS`` rows a step, bound (and
    agreed on) like the target's runner."""

    def __init__(self, draft, atom_config, spec_steps: int) -> None:
        tp = get_tp_group()
        self.group = tp.cpu_group
        self.tp, self.rank = tp.world_size, tp.rank_in_group
        self.T = spec_steps + 1
        self.max_seq = atom_config.max_model_len + 1
        bind_agreed(lambda: self._bind_draft(draft), self.group)
        # step 0 (T rows a request) and steps 1.. (one row a request)
        self.by_t = {
            t: WidthBuilds(
                lambda S, t=t: self._build_draft_width(S, t),
                self._width_kernels,
                self.group,
                f"Qwen3.5 mono draft (T {t})",
            )
            for t in (self.T, 1)
        }

    def _bind_draft(self, draft) -> None:
        self.mtp = draft.model
        if len(self.mtp.layers) != 1:
            raise MonoUnsupported(f"{len(self.mtp.layers)} MTP layers: one served")
        layer = self.mtp.layers[0]
        if layer.layer_type != "full_attention":
            raise MonoUnsupported(f"MTP layer {layer.layer_type}")
        self.dev = torch.device("cuda", torch.cuda.current_device())
        cus = torch.cuda.get_device_properties(self.dev).multi_processor_count
        if cus != BLOCKS:
            raise MonoUnsupported(
                f"{cus} CUs: the kernel needs all {BLOCKS} CTAs co-resident"
            )
        self.cfg = Q35Config(
            tp=self.tp,
            rank=self.rank,
            qk_fusion=envs.ATOM_ENABLE_QK_NORM_ROPE_CACHE_QUANT_FUSION,
        )
        self.full = [True]
        self.layers = [(LayerWeights(self.cfg, True), self._pack(0, layer))]
        self.layer_nums = [layer.self_attn.attn.layer_num]
        # positions < max_model_len
        rope = rope_tables(layer.self_attn.rotary_emb, self.cfg, self.max_seq - 1)
        if rope is None:
            raise MonoUnsupported(
                f"the RoPE caches do not cover {self.max_seq - 1} positions"
            )
        self.cos, self.sin = rope
        logger.info("Qwen3.5 mono draft: rank %d bound the MTP layer", self.rank)

    def _build_draft_width(self, S: int, T: int) -> dict:
        W, p = self.layers[0]
        var = Qwen35Variant(
            self.cfg, S, True, self.rank, self.tp, self.group, tokens_per_seq=T
        )
        op = Qwen35MonoKernel(
            W, S, self.rank, self.tp, variant=var, packed=p, tokens_per_seq=T
        )
        torch.cuda.synchronize()
        return {"variants": {True: var}, "ops": [op]}

    def step_t(self, rows: int) -> int | None:
        """This draft step's tokens a request: step 0 runs the verify rows, steps 1.. one row a
        request; None for a step mono does not serve."""
        bs = get_forward_context().context.scheduled_bs
        if rows == bs * self.T and rows <= MAX_TOKENS:
            return self.T
        if rows == bs and rows <= MAX_TOKENS:
            return 1
        return None

    def prepare(self, rows: int) -> bool:
        t = self.step_t(rows)
        return t is not None and self.by_t[t].prepare(rows)

    def layer(self, x: torch.Tensor, positions: torch.Tensor) -> torch.Tensor:
        """The decoder layer on ``x`` [rows, hidden] (the ``fc`` output, gathered): the f32 residual
        stream after it (the draft's final norm is the caller's)."""
        md = get_forward_context().attn_metadata
        S = x.shape[0]
        T = self.step_t(S)
        pos = (positions[0] if positions.dim() == 2 else positions)[:S].to(torch.int32)
        slots = md.slot_mapping[:S].to(torch.int32)
        bt = md.block_tables[: S // T]
        if T > 1:
            bt = bt.repeat_interleave(T, 0)
        lc = get_forward_context().kv_cache_data[f"layer_{self.layer_nums[0]}"]
        built = self.by_t[T][S]
        kv = (lc.k_cache, lc.v_cache, lc.k_scale, lc.v_scale, pos, bt, slots)
        out = built["ops"][0].forward(
            x.float().contiguous(),
            kv=kv,
            rope=(self.cos, self.sin),
            layer=0,
            advance=False,
        )
        built["variants"][True].advance_step()
        return out
