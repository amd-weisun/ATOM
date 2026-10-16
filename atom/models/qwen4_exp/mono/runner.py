# SPDX-License-Identifier: MIT
# Copyright (C) 2026, Advanced Micro Devices, Inc. All rights reserved.
"""One decode step of up to ``MAX_TOKENS`` rows on the Qwen3.8 layer kernel: every decoder layer one
launch (``op.Qwen38MonoKernel``), over ATOM's own weights and state, at TP1.

The residual is the hyper-connection bundle (``hc`` streams of the hidden size, bf16): the embedding
repeated into every stream, each launch completing its layer's combines. layers.1's per-layer embedding
(PLE) runs on ATOM's module between launches, as the original layer adds it before its mix; the final
mixer is ATOM's, or, with a drafter, the bundle itself is returned (the mixer then runs in
``compute_logits``). State is ATOM's: the GDN conv / recurrent pools and their slots, the QSA module's
paged caches (K / V, raw and compressed index keys), block tables and slot mapping, read and written in
place, so a sequence can switch paths between steps. The weights are ATOM's tensors in place.

A step launches at its own width (rows): a plain decode one row a request, an MTP verify ``T =
spec_steps + 1`` rows a request on the snapshot GDN state (a state slot a drafted token). A width's
kernels and scratch are built on its first step (``WidthBuilds``); the layers of one kind (GDN, QSA)
share them.
"""

import inspect
import logging

import torch
from aiter.dist.parallel_state import get_tp_group

from atom.models.qwen4_exp.mono.config import MAX_TOKENS, Q38Config, is_full_attention
from atom.models.qwen4_exp.mono.op import Qwen38MonoKernel, Qwen38Variant
from atom.models.qwen4_exp.mono.weights import LayerWeights, rope_tables
from atom.mono.plan.execution import BLOCKS
from atom.mono.runtime.abi import KernelAbi
from atom.mono.runtime.consensus import MonoUnsupported, bind_agreed
from atom.mono.runtime.widths import WidthBuilds
from atom.utils.forward_context import get_forward_context

logger = logging.getLogger("atom")


def _zero_abi(launcher) -> KernelAbi:
    """A launcher's parameters but ``stream``: what a compile-only call zeroes."""
    fn = getattr(launcher, "func", None) or launcher._func
    params = tuple(inspect.signature(fn).parameters)
    assert params[-1] == "stream", params
    return KernelAbi(params[:-1])


class Qwen38MonoDecodeRunner:
    """Owns the per-layer weight views (bound at install, before the KV cache is sized) and, per
    launch width, the compiled kernels and their scratch."""

    def __init__(
        self, causal_lm, atom_config, spec_steps: int, replay_cap: int = 0
    ) -> None:
        tp = get_tp_group()
        self.group = tp.cpu_group
        # an MTP verify: T rows a request, the anchor and its drafted tokens
        self.T = spec_steps + 1 if spec_steps else 1
        self.replay_cap = replay_cap
        self.max_seq = atom_config.max_model_len + 1
        bind_agreed(lambda: self._bind(causal_lm), self.group)
        self.widths = WidthBuilds(
            self._build_width, self._width_kernels, self.group, "Qwen3.8 mono"
        )

    # ------------------------------------------------------------------ setup
    def _bind(self, causal_lm) -> None:
        """The checks and weight views; raises ``MonoUnsupported``."""
        if self.replay_cap:
            raise MonoUnsupported(
                "ReplaySSM GDN state under MTP (the kernel keeps the snapshot state: "
                "ATOM_ENABLE_REPLAYSSM=0)"
            )
        self.lm = causal_lm.model
        self.dev = torch.device("cuda", torch.cuda.current_device())
        cus = torch.cuda.get_device_properties(self.dev).multi_processor_count
        if cus != BLOCKS:
            raise MonoUnsupported(
                f"{cus} CUs: the kernel needs all {BLOCKS} CTAs co-resident"
            )
        self.cfg = Q38Config()
        n = len(self.lm.layers)
        self.full = [is_full_attention(i) for i in range(n)]
        for i, layer in enumerate(self.lm.layers):
            if (layer.layer_type == "full_attention") != self.full[i]:
                raise MonoUnsupported(f"layer {i}: {layer.layer_type}")
            m = layer.mlp
            if m.experts.w13_weight.dtype != torch.float8_e4m3fn:
                raise MonoUnsupported(
                    f"layer {i}: routed experts {m.experts.w13_weight.dtype}, not FP8"
                )
            if getattr(m, "shared_expert", None) is None:
                raise MonoUnsupported(
                    f"layer {i}: the shared expert fused into the bank (the kernel runs it apart, BF16)"
                )
        self.packed = [
            self._pack(i, layer, self.full[i]) for i, layer in enumerate(self.lm.layers)
        ]
        self.layer_nums = [
            None if self.full[i] else layer.linear_attn.attn.layer_num
            for i, layer in enumerate(self.lm.layers)
        ]
        rope = rope_tables(
            next(
                layer for i, layer in enumerate(self.lm.layers) if self.full[i]
            ).self_attn.rotary_emb,
            self.cfg,
            self.max_seq - 1,  # positions < max_model_len
        )
        if rope is None:
            raise MonoUnsupported(
                f"the RoPE caches do not cover {self.max_seq - 1} positions"
            )
        self.cos, self.sin = rope
        logger.info("Qwen3.8 mono: bound %d layers", n)

    @staticmethod
    def _pack(i, layer, full) -> dict:
        """The kernel's weight dict over ATOM's tensors of decoder layer ``i``."""

        def f32(t):
            return t.detach().float().contiguous()

        p = {}
        for side, hc in (
            ("hca", layer.attn_hyper_connection),
            ("hcm", layer.mlp_hyper_connection),
        ):
            p[f"{side}_g"] = 1.0 + f32(hc.hc_norm.weight)
            p[f"{side}_down"] = hc.input_mix_weight_down.weight
            p[f"{side}_up"] = hc.input_mix_weight_up.weight
            p[f"{side}_inj"] = hc.block_inject_weight.weight
        if full:
            a = layer.self_attn
            p.update(
                w_qkv=a.qkv_proj.weight,
                w_idx=a.indexer.index_qk_proj.weight,
                w_out=a.o_proj.weight,
                g_qn1=1.0 + f32(a.q_norm.weight),
                g_kn1=1.0 + f32(a.k_norm.weight),
                g_iq1=1.0 + f32(a.indexer.q_layernorm.weight),
                g_ik1=1.0 + f32(a.indexer.k_layernorm.weight),
            )
        else:
            a = layer.linear_attn
            p.update(
                w_proj=a.in_proj_qkvzba.weight,
                w_conv=a.conv1d.weight,
                a_log=f32(a.A_log),
                dt_bias=f32(a.dt_bias),
                g_norm=f32(a.norm.weight),
                w_out=a.out_proj.weight,
            )
        m = layer.mlp
        p.update(
            w_r=m.gate.weight,
            w_ug=m.experts.w13_weight,
            s_ug=m.experts.w13_weight_scale,
            w_dn=m.experts.w2_weight,
            s_dn=m.experts.w2_weight_scale,
            w_sh_ug=m.shared_expert.gate_up_proj.weight,
            w_sh_dn=m.shared_expert.down_proj.weight,
        )
        for k, t in p.items():
            if not t.is_contiguous():
                raise MonoUnsupported(
                    f"layer {i}: ATOM tensor {k} not contiguous {tuple(t.shape)}"
                )
        return p

    def _build_width(self, S: int) -> dict:
        """The S-row launch: a variant (kernel, scratch) per layer kind and an op per layer."""
        T = self.T
        variants = {
            full: Qwen38Variant(
                self.cfg, S, full, max_seq=self.max_seq, tokens_per_seq=T
            )
            for full in (False, True)
        }
        ops = [
            Qwen38MonoKernel(
                LayerWeights(self.cfg, self.full[i]),
                S,
                variant=variants[self.full[i]],
                packed=p,
                tokens_per_seq=T,
            )
            for i, p in enumerate(self.packed)
        ]
        return {"variants": variants, "ops": ops}

    @staticmethod
    def _width_kernels(built: dict) -> list:
        out = []
        for var in built["variants"].values():
            out.append((var.launch, _zero_abi(var.launch)))
            out.append((var._advance, _zero_abi(var._advance)))
        return out

    def launch_width(self, rows: int) -> int | None:
        """The launch width of a ``rows``-row step, or None (not served): whole requests' rows."""
        return rows if 0 < rows <= MAX_TOKENS and rows % self.T == 0 else None

    def prepare(self, rows: int) -> bool:
        """Whether the launch of a ``rows``-row step is built."""
        S = self.launch_width(rows)
        return S is not None and self.widths.prepare(S)

    # ---------------------------------------------------------------- forward
    def forward(self, input_ids: torch.Tensor, positions: torch.Tensor) -> torch.Tensor:
        """The mixed hidden states [rows, hidden] (or, with a drafter, the bundle [rows, hc, hidden]),
        as the model returns."""
        fwd = get_forward_context()
        md, kvd = fwd.attn_metadata, fwd.kv_cache_data
        S = input_ids.numel()
        pos = (positions[0] if positions.dim() == 2 else positions)[:S].to(torch.int32)
        g = md.gdn_metadata
        sm = md.slot_mapping
        if sm.numel() < S:
            # GDN's verify-graph capture metadata slices the per-token slot buffer to the sequence
            # count; the buffer itself holds every token's slot
            assert (
                sm.is_contiguous()
                and sm.untyped_storage().nbytes()
                >= (sm.storage_offset() + S) * sm.element_size()
            )
            sm = sm.as_strided((S,), (1,))
        slots = sm[:S].to(torch.int32)
        T = self.T
        if T == 1:
            gdn = (
                g.non_spec_state_indices_in_tensor[:S].to(torch.int32),
                g.non_spec_state_indices_tensor[:S].to(torch.int32),
            )
            bt = md.block_tables[:S]
        else:
            Q = S // T
            gdn = (
                g.num_accepted_tokens[:Q].to(torch.int32),
                g.spec_state_indices_tensor[:Q].reshape(-1).to(torch.int32),
            )
            bt = md.block_tables[:Q].repeat_interleave(T, 0)
        built = self.widths[S]
        X = self.lm.embed_tokens(input_ids).repeat(1, self.cfg.hc).contiguous()
        for i, (op, layer) in enumerate(zip(built["ops"], self.lm.layers)):
            if layer.ple is not None:
                # the original layer adds the PLE output before its mix (two bf16 adds)
                X = (
                    X + layer.ple.forward_with_state(X, input_ids, md.ple_metadata)
                ).contiguous()
            if op.full:
                a = layer.self_attn  # the QSA module owns its paged caches
                kv = (
                    a.k_cache,
                    a.v_cache,
                    a.raw_key_cache,
                    a.compressed_key_cache,
                    pos,
                    bt,
                    slots,
                )
                X = op.forward(
                    X, kv=kv, rope=(self.cos, self.sin), layer=i, advance=False
                )
            else:
                lc = kvd[f"layer_{self.layer_nums[i]}"]
                X = op.forward(
                    X, gdn_state=(lc.k_cache, lc.v_cache) + gdn, layer=i, advance=False
                )
        for var in built["variants"].values():
            var.advance_step()
        if (
            self.lm.return_hc_state
        ):  # a drafter consumes the bundle; the mixer runs in compute_logits
            return X.view(-1, self.cfg.hc, self.cfg.hidden)
        mixed, _ = self.lm.hyper_connection_mixer.mix_fused(X, None)
        return mixed
