# SPDX-License-Identifier: MIT
# Copyright (C) 2026, Advanced Micro Devices, Inc. All rights reserved.
"""A Qwen3.8-Flash-Next decoder layer as the kernel builds for it (``LayerWeights``: its shape and kind) and
the RoPE tables the QSA layers read."""

from __future__ import annotations

from dataclasses import dataclass

from atom.models.qwen4_exp.mono.config import Q38Config


@dataclass
class LayerWeights:
    cfg: Q38Config
    full: bool  # QSA; else Gated DeltaNet


def rope_tables(rotary_emb, cfg: Q38Config, max_pos: int):
    """The kernel's RoPE tables: ATOM's own (aiter's ``RotaryEmbedding`` caches, model dtype, the QSA
    module's) as f32 cos, sin [max_pos, rope_dim / 2], or None when they do not cover ``max_pos``.
    """
    cos = rotary_emb.cos_cache.reshape(rotary_emb.cos_cache.shape[0], -1)
    sin = rotary_emb.sin_cache.reshape(rotary_emb.sin_cache.shape[0], -1)
    if cos.shape[0] < max_pos or cos.shape[1] != cfg.rope_dim // 2:
        return None
    return cos[:max_pos].float().contiguous(), sin[:max_pos].float().contiguous()
