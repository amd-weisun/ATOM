# SPDX-License-Identifier: MIT
# Copyright (C) 2026, Advanced Micro Devices, Inc. All rights reserved.
"""Host wrapper: scratch and the launch of one Qwen3.8-Flash-Next decoder layer (TP1; Gated DeltaNet or QSA,
then the MoE, between hyper-connection reads) over ATOM's weights and state."""

from __future__ import annotations

from typing import ClassVar

import torch

from atom.models.qwen3_5.mono.kernels.step import build_advance_step
from atom.models.qwen4_exp.mono.kernels.layer import build_qwen38_kernel
from atom.models.qwen4_exp.mono.layout import layout, shape_dims
from atom.models.qwen4_exp.mono.weights import LayerWeights

__all__ = ["Qwen38MonoKernel", "Qwen38Variant"]


class Qwen38Variant:
    """The compiled launch, scratch and step counter that every layer of one kind (GDN or QSA) shares at one
    sample count. Fresh epoch tags (``step * LAYER_SLOTS + layer + 1``) keep layers apart in the shared scratch:
    one ``advance_step`` per decode step, distinct ``layer`` values per launch."""

    _cache: ClassVar[dict] = {}
    _adv_cache: ClassVar[dict] = {}

    def __init__(
        self,
        cfg,
        samples: int,
        full: bool,
        max_seq: int = 4096,
        tokens_per_seq: int = 1,
    ):
        if samples % tokens_per_seq:
            raise ValueError(
                "an MTP verify launch holds whole sequences of tokens_per_seq rows"
            )
        self.cfg, self.S, self.full, self.T = cfg, samples, full, tokens_per_seq
        dims = shape_dims(cfg, max_seq)
        dev = torch.device("cuda", torch.cuda.current_device())
        self.scr_layout = layout(samples, full, **dims)
        self.scratch = torch.zeros(
            self.scr_layout["_bytes"], dtype=torch.uint8, device=dev
        )
        self.step = torch.zeros(1, dtype=torch.int32, device=dev)
        self.key = (samples, full, tokens_per_seq, tuple(sorted(dims.items())))
        if self.key not in Qwen38Variant._cache:
            Qwen38Variant._cache[self.key] = build_qwen38_kernel(
                samples, full, tokens_per_seq=tokens_per_seq, **dims
            )
        self.launch = Qwen38Variant._cache[self.key]
        # the step advance clears stale mailbox pairs of the scratch (no symmetric buffer at TP1)
        adv_key = self.scr_layout["_bytes"] // 8
        if adv_key not in Qwen38Variant._adv_cache:
            Qwen38Variant._adv_cache[adv_key] = build_advance_step(adv_key, 0)
        self._advance = Qwen38Variant._adv_cache[adv_key]

    def advance_step(self):
        """``step += 1`` and clear a slice of stale mailbox pairs; graph-capturable."""
        self._advance(
            self.step.data_ptr(),
            self.scratch.data_ptr(),
            0,
            stream=torch.cuda.current_stream(),
        )


class Qwen38MonoKernel:
    """One decoder layer: ``packed``, the kernel's view of ATOM's weights, run on a ``Qwen38Variant`` of its kind
    and width.

    State belongs to the caller (ATOM's pools): a GDN layer's conv / fp32 recurrent state and its slots; a QSA
    layer's paged BF16 K / V, index and compressed-key caches, positions, block tables and slot mapping, and the
    RoPE tables."""

    def __init__(
        self,
        W: LayerWeights,
        samples: int,
        variant: Qwen38Variant,
        packed: dict,
        tokens_per_seq: int = 1,
    ):
        if (variant.S, variant.full, variant.T) != (samples, W.full, tokens_per_seq):
            raise ValueError(
                "the variant was built for another sample count or layer kind"
            )
        self.W, self.S, self.full = W, samples, W.full
        self.packed = packed
        self.variant = variant

    def forward(
        self,
        X,
        *,
        gdn_state=None,
        kv=None,
        rope=None,
        x_out=None,
        layer=0,
        advance=True,
    ):
        """One layer on the bundle ``X`` [S, hc * hidden] bf16 -> the next bundle; updates the state in place.

        GDN: ``gdn_state = (conv_state [slots, 3, conv_dim] bf16, ssm_state [slots, hv, k, v] fp32, slots_in,
        slots_out)``; an MTP verify variant (``tokens_per_seq`` T > 1) takes ``(conv_state, ssm_state,
        n_accepted, spec_slots)``: per sequence the tokens accepted last step, per row the state slot after its
        token, the conv window [slots, T + 2, conv_dim] in each sequence's first slot. QSA: ``kv = (k_cache,
        v_cache [blocks, 16, kv_heads, head_dim] bf16, index_cache [blocks, 16, 1, index_dim] bf16,
        compressed_cache [blocks, 4, 1, index_dim] bf16, positions, block_tables, slot_mapping)`` (ATOM's QSA
        pool) and ``rope = (cos, sin)``, f32 ``[positions, rope_dim / 2]`` (``weights.rope_tables``).
        """
        cfg, S = self.W.cfg, self.S
        if (
            tuple(X.shape) != (S, cfg.streams)
            or X.dtype != torch.bfloat16
            or not X.is_contiguous()
        ):
            raise ValueError(f"X must be contiguous bfloat16 {(S, cfg.streams)}")

        def i32(name, t, shape):
            if (
                tuple(t.shape[: len(shape)]) != shape
                or t.dtype != torch.int32
                or t.stride(-1) != 1
            ):
                raise ValueError(f"{name} must be int32 {shape} with unit-stride rows")

        conv = ssm = sl_in = sl_out = n_acc = None
        kc = vc = ic = cc = pos = bt = slots = cos = sin = None
        bt_stride = 0
        if self.full:
            kc, vc, ic, cc, pos, bt, slots = kv
            cos, sin = rope
            for name, t, shp in (
                ("k_cache", kc, (16, cfg.kv_heads, cfg.head_dim)),
                ("index_cache", ic, (16, 1, cfg.index_dim)),
                ("compressed_cache", cc, (16 // cfg.index_ratio, 1, cfg.index_dim)),
            ):
                if (
                    t.dtype != torch.bfloat16
                    or tuple(t.shape[1:]) != shp
                    or not t.is_contiguous()
                ):
                    raise ValueError(
                        f"{name} must be contiguous bf16 [blocks, {shp}] (ATOM's QSA pool)"
                    )
            i32("positions", pos, (S,))
            i32("slot_mapping", slots, (S,))
            i32("block_tables", bt, (S,))
            bt_stride = bt.stride(0)
        else:
            T = self.variant.T
            if T == 1:
                conv, ssm, sl_in, sl_out = gdn_state
                sl_out = sl_in if sl_out is None else sl_out
            else:
                conv, ssm, n_acc, sl_out = gdn_state
                sl_in = sl_out
                i32("n_accepted", n_acc, (S // T,))
            width = cfg.conv_kernel - 1 if T == 1 else T + 2
            if conv.dtype != torch.bfloat16 or tuple(conv.shape[1:]) != (
                width,
                cfg.conv_dim,
            ):
                raise ValueError(
                    f"conv_state must be bf16 [slots, {width}, {cfg.conv_dim}]"
                )
            if ssm.dtype != torch.float32 or tuple(ssm.shape[1:]) != (
                cfg.linear_v_heads,
                cfg.linear_k_dim,
                cfg.linear_v_dim,
            ):
                raise ValueError(
                    "ssm_state must be fp32 [slots, hv, k_dim, v_dim] (K-major, as ATOM)"
                )
            i32("slots_in", sl_in, (S,))
            i32("slots_out", sl_out, (S,))
        if x_out is None:
            x_out = torch.empty_like(X)
        p = self.packed

        def ptr(x):
            return 0 if x is None else x.data_ptr()

        def ptr0(k):
            return p[k].data_ptr() if k in p else 0

        v = self.variant
        v.launch(
            ptr(X),
            ptr(x_out),
            ptr(sl_in),
            ptr(sl_out),
            ptr(n_acc),
            ptr(conv),
            ptr(ssm),
            ptr(pos),
            ptr(bt),
            ptr(slots),
            ptr(kc),
            ptr(vc),
            ptr(ic),
            ptr(cc),
            ptr(cos),
            ptr(sin),
            ptr0("g_qn1"),
            ptr0("g_kn1"),
            ptr0("g_iq1"),
            ptr0("g_ik1"),
            ptr0("w_qkv"),
            ptr0("w_idx"),
            ptr(p["hca_g"]),
            ptr(p["hca_down"]),
            ptr(p["hca_up"]),
            ptr(p["hca_inj"]),
            ptr(p["hcm_g"]),
            ptr(p["hcm_down"]),
            ptr(p["hcm_up"]),
            ptr(p["hcm_inj"]),
            ptr0("w_proj"),
            ptr0("w_conv"),
            ptr0("a_log"),
            ptr0("dt_bias"),
            ptr0("g_norm"),
            ptr(p["w_out"]),
            ptr(p["w_r"]),
            ptr(p["w_ug"]),
            ptr(p["s_ug"]),
            ptr(p["w_dn"]),
            ptr(p["s_dn"]),
            ptr(p["w_sh_ug"]),
            ptr(p["w_sh_dn"]),
            ptr(v.scratch),
            ptr(v.step),
            layer,
            bt_stride,
            stream=torch.cuda.current_stream(),
        )
        if advance:
            self.advance_step()
        return x_out

    def advance_step(self):
        self.variant.advance_step()
