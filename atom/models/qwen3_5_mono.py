# SPDX-License-Identifier: MIT
"""Bench-only (qwen_mono_bench): Qwen3.5-MoE decode on the FlyDSL MonoKernel, one launch per layer.

``QWEN_MONO=1`` wraps the loaded model (``install_mono_decode``, registered in ``mono_decode_installers``).
A decode step of at most ``MAX_ROWS`` rows (a captured graph's padded width) runs embed -> 60 layer
launches -> final norm; every other forward (prefill, larger batches) is the original model's.

State is ATOM's own: GDN conv / ssm pools and state slots, the paged FP8 KV cache with its block tables
and slot mapping. Weights are re-read from the checkpoint and packed at install time (before the KV
cache is sized): a development setup that duplicates them, not the production one.
"""

import logging
import os
import sys

import torch
from torch import nn

logger = logging.getLogger("atom")
MAX_ROWS = 8
_HARNESS = os.environ.get("QWEN_MONO_HARNESS", "/home/weisu/qwen_repro/run")


def _lm(model):
    """The module holding embed_tokens / layers / norm."""
    for m in model.modules():
        if all(hasattr(m, a) for a in ("embed_tokens", "layers", "norm")):
            return m
    raise RuntimeError("no embed_tokens / layers / norm in the model")


class _Runner:
    def __init__(self, model, atom_config):
        from aiter.dist.parallel_state import get_tp_group

        from kernels.monokernel.qwen35.config import Q35Config, is_full_attention
        from kernels.monokernel.qwen35.op import Qwen35MonoKernel, Qwen35Variant, pack_layer_weights
        from kernels.monokernel.qwen35.reference import rope_tables

        sys.path.insert(0, _HARNESS)
        from q35_ckpt import _Ckpt, load_layer

        tp = get_tp_group()
        self.rank, self.npes, group = tp.rank_in_group, tp.world_size, tp.cpu_group
        self.lm = _lm(model)
        self.cfg = Q35Config(tp=self.npes, rank=self.rank)
        n = len(self.lm.layers)
        self.full = [is_full_attention(i) for i in range(n)]
        ck = _Ckpt(atom_config.model)
        self.packed = []
        for i in range(n):
            W = load_layer(self.cfg, i, self.full[i], device="cuda", ckpt=ck)  # slices straight to the GPU
            self.packed.append((W, pack_layer_weights(W)))
            del W.t["w_ug"], W.t["w_dn"]  # the packed copies are what the kernel reads
            if i % 10 == 9 and self.rank == 0:
                logger.info("Qwen3.5 mono decode: %d / %d layers packed", i + 1, n)
        torch.cuda.empty_cache()
        self.cos, self.sin = rope_tables(self.cfg, atom_config.max_model_len + 1)
        self.variants, self.layers = {}, {}
        for S in range(1, MAX_ROWS + 1):
            for full in (False, True):
                self.variants[(S, full)] = Qwen35Variant(self.cfg, S, full, self.rank, self.npes, group)
            self.layers[S] = [
                Qwen35MonoKernel(W, S, self.rank, self.npes, variant=self.variants[(S, self.full[i])], packed=p)
                for i, (W, p) in enumerate(self.packed)
            ]
        self._layer_nums = [self._layer_num(layer) for layer in self.lm.layers]
        self._warm()
        logger.info("Qwen3.5 mono decode ready: %d layers, rank %d / %d", n, self.rank, self.npes)

    @staticmethod
    def _layer_num(layer):
        mod = layer.linear_attn.attn if layer.layer_type == "linear_attention" else layer.self_attn.attn
        return mod.layer_num

    def _warm(self):
        """Compile every variant with a dry launch (no state written: every slot is -1)."""
        cfg, dev = self.cfg, "cuda"
        conv = torch.zeros(1, 3, cfg.conv_dim, dtype=torch.bfloat16, device=dev)
        ssm = torch.zeros(1, cfg.hv, cfg.linear_k_dim, cfg.linear_v_dim, dtype=torch.bfloat16, device=dev)
        kc = torch.zeros(1, 1, cfg.head_dim // 16, 16, 16, dtype=torch.float8_e4m3fn, device=dev)
        vc = torch.zeros(1, 1, 1, cfg.head_dim, 16, dtype=torch.float8_e4m3fn, device=dev)
        sc = torch.zeros(1, 1, 16, dtype=torch.float32, device=dev)  # ATOM sets the default dtype to bf16
        for S in range(1, MAX_ROWS + 1):
            neg = torch.full((S,), -1, dtype=torch.int32, device=dev)
            zero = torch.zeros(S, dtype=torch.int32, device=dev)
            bt = torch.zeros(S, 1, dtype=torch.int32, device=dev)
            h = torch.zeros(S, cfg.hidden, dtype=torch.bfloat16, device=dev)
            done = set()
            for i, L in enumerate(self.layers[S]):
                if L.full in done:
                    continue
                done.add(L.full)
                if L.full:
                    L.forward(h, kv=(kc, vc, sc, sc, zero, bt, neg), rope=(self.cos, self.sin), layer=i, advance=False)
                else:
                    L.forward(h, gdn_state=(conv, ssm, neg, neg), layer=i, advance=False)
            for full in (False, True):
                self.variants[(S, full)].advance_step()
        torch.cuda.synchronize()

    def forward(self, input_ids, positions):
        from atom.utils.forward_context import get_forward_context

        fwd = get_forward_context()
        md, kvd = fwd.attn_metadata, fwd.kv_cache_data
        S = input_ids.numel()
        pos = (positions[0] if positions.dim() == 2 else positions)[:S].to(torch.int32)
        g = md.gdn_metadata
        sl_in = g.non_spec_state_indices_in_tensor[:S].to(torch.int32)
        sl_out = g.non_spec_state_indices_tensor[:S].to(torch.int32)
        bt = md.block_tables[:S]
        slots = md.slot_mapping[:S].to(torch.int32)
        x = self.lm.embed_tokens(input_ids)
        for i, L in enumerate(self.layers[S]):
            lc = kvd[f"layer_{self._layer_nums[i]}"]
            if L.full:
                kv = (lc.k_cache, lc.v_cache, lc.k_scale, lc.v_scale, pos, bt, slots)
                x = L.forward(x, kv=kv, rope=(self.cos, self.sin), layer=i, advance=False)
            else:
                x = L.forward(x, gdn_state=(lc.k_cache, lc.v_cache, sl_in, sl_out), layer=i, advance=False)
        for full in (False, True):
            self.variants[(S, full)].advance_step()
        out = self.lm.norm(x)
        return out[0] if isinstance(out, tuple) else out


def step_supported(input_ids, inputs_embeds) -> bool:
    from atom.utils.forward_context import get_forward_context

    rows = input_ids.numel()
    if inputs_embeds is not None or not 0 < rows <= MAX_ROWS:
        return False
    fwd = get_forward_context()
    if fwd.context is None or fwd.context.is_prefill or getattr(fwd, "ubatch_slices", None) is not None:
        return False
    g = getattr(fwd.attn_metadata, "gdn_metadata", None)
    return g is not None and g.spec_sequence_masks is None and g.num_prefills == 0


class MonoDecodeModel(nn.Module):
    def __init__(self, model, atom_config):
        super().__init__()
        self.model = model
        self._mono = _Runner(model, atom_config)

    def forward(self, input_ids, positions, intermediate_tensors=None, inputs_embeds=None, **kw):
        if step_supported(input_ids, inputs_embeds):
            return self._mono.forward(input_ids, positions)
        return self.model(input_ids, positions, intermediate_tensors, inputs_embeds, **kw)

    def compute_logits(self, hidden_states):
        return self.model.compute_logits(hidden_states)

    def __getattr__(self, name):
        try:
            return super().__getattr__(name)
        except AttributeError:
            return getattr(self.model, name)


def install_mono_decode(model, atom_config, drafter, metadata_builder):
    if os.environ.get("QWEN_MONO") != "1":
        return model
    if drafter is not None:
        logger.info("Qwen3.5 mono decode off: speculative decoding is not supported yet")
        return model
    return MonoDecodeModel(model, atom_config)
