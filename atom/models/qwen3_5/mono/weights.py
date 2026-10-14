# SPDX-License-Identifier: MIT
# Copyright (C) 2026, Advanced Micro Devices, Inc. All rights reserved.
"""A Qwen3.5 decoder layer as the kernel builds for it (``LayerWeights``: its TP shard's shape and
kind) and the RoPE tables the full-attention layers read."""

from __future__ import annotations

from dataclasses import dataclass

import torch

from atom.models.qwen3_5.mono.config import Q35Config


@dataclass
class LayerWeights:
    cfg: Q35Config
    full: bool  # gated full attention; else Gated DeltaNet


def rope_cos_sin(positions: torch.Tensor, cfg: Q35Config):
    """cos/sin [S, rope_dim] (halves repeated, rotate-half convention)."""
    inv = 1.0 / (
        cfg.rope_theta
        ** (
            torch.arange(0, cfg.rope_dim, 2, device=positions.device).float()
            / cfg.rope_dim
        )
    )
    f = positions.float()[:, None] * inv[None]
    f = torch.cat([f, f], -1)
    return f.cos(), f.sin()


def rope_tables(cfg: Q35Config, max_pos: int, device="cuda"):
    """The kernel's RoPE tables: cos, sin f32 [max_pos, rope_dim / 2] (the rotate-half halves are equal)."""
    cos, sin = rope_cos_sin(torch.arange(max_pos, device=device), cfg)
    half = cfg.rope_dim // 2
    return cos[:, :half].contiguous(), sin[:, :half].contiguous()
