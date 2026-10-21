# SPDX-License-Identifier: MIT
# Copyright (C) 2026, Advanced Micro Devices, Inc. All rights reserved.
"""The final Gemma RMSNorm of the layers' f32 residual stream -> bf16, one launch (a CTA a row).

The arithmetic is the layer kernel's input norm (``common.stage_x_gemma``): each thread's sum of
squares over its 4-element groups in order, a wave sum, the waves added in order, ``rsq(sum / n + eps)``,
``x * rstd * (1 + w)`` in f32 and one bf16 rounding -- what the original path's fused all-reduce norm
computes on ``residual + bf16(rank sum)``.
"""

import functools

import flydsl.compiler as flyc
import flydsl.expr as fx
import torch
from aiter.ops.flydsl.kernels import buffer_ops as bo
from flydsl.expr import const_expr, gpu, range_constexpr
from flydsl.expr.typing import Int32, Int64, T

from atom.models.qwen3_5.mono.kernels.ops import lds_ld, lds_st
from atom.mono.device.ops import (
    bf16_pair,
    kernel_symbol,
    launcher,
    rsq,
    rsrc,
    wave_sum,
)
from atom.mono.plan.execution import THREADS, WAVES

_CURRENT_STREAM = fx.Stream(None)


@functools.cache
def _build(hidden: int, eps: float, bf16_weight: bool):
    assert hidden % (4 * THREADS) == 0
    PER = hidden // (4 * THREADS)  # 4-element groups a thread
    name = kernel_symbol("qwen35_final_norm", n=hidden, wbf16=int(bf16_weight))

    @fx.struct
    class Smem:
        red: fx.Array[fx.Float32, WAVES, 16]

    @flyc.kernel(name=name, known_block_size=[THREADS, 1, 1])
    def final_norm(x: Int64, weight: Int64, out: Int64):
        tid = fx.thread_idx.x
        row = fx.block_idx.x
        red = fx.SharedAllocator().allocate(Smem).peek().red.ptr
        rx, rw, ro = rsrc(x), rsrc(weight), rsrc(out)
        ks = [(tid + i * THREADS) * 4 for i in range(PER)]
        xs = [
            fx.Vector(bo.buffer_load(rx, row * hidden + k, vec_width=4, dtype=T.f32))
            for k in ks
        ]
        if const_expr(bf16_weight):
            ws = [
                fx.Vector(bo.buffer_load(rw, k, vec_width=4, dtype=T.bf16)).to(
                    fx.Float32
                )
                for k in ks
            ]
        else:
            ws = [
                fx.Vector(bo.buffer_load(rw, k, vec_width=4, dtype=T.f32)) for k in ks
            ]
        ss = fx.Float32(0.0)
        for i in range_constexpr(PER):
            for e in range_constexpr(4):
                ss = ss + xs[i][e] * xs[i][e]
        w = wave_sum(ss)
        if tid % 64 == 0:
            lds_st(red, tid // 64, w)
        gpu.barrier()
        tot = lds_ld(red, 0)
        for i in range_constexpr(1, WAVES):
            tot = tot + lds_ld(red, i)
        rstd = rsq(tot * (1.0 / hidden) + eps)
        for i in range_constexpr(PER):
            g = [1.0 + ws[i][e] for e in range(4)]
            words = [
                bf16_pair(
                    xs[i][2 * j] * rstd * g[2 * j],
                    xs[i][2 * j + 1] * rstd * g[2 * j + 1],
                )
                for j in range(2)
            ]
            bo.buffer_store(
                fx.Vector.from_elements(words, fx.Float32),
                ro,
                (row * hidden + ks[i]) // 2,
            )

    @launcher(name)
    def launch(
        x: Int64,
        weight: Int64,
        out: Int64,
        rows: Int32,
        stream: fx.Stream = _CURRENT_STREAM,
    ):
        final_norm(x, weight, out).launch(grid=(rows,), block=(THREADS,), stream=stream)

    return launch


def final_norm(norm, x: torch.Tensor) -> torch.Tensor:
    """``norm`` (a GemmaRMSNorm) of the f32 rows ``x`` [rows, hidden] -> bf16, one launch."""
    w = norm.weight.data
    assert x.dtype == torch.float32 and x.is_contiguous()
    assert w.dtype in (torch.bfloat16, torch.float32)
    out = torch.empty(x.shape, dtype=torch.bfloat16, device=x.device)
    _build(x.shape[-1], float(norm.variance_epsilon), w.dtype == torch.bfloat16)(
        x.data_ptr(),
        w.data_ptr(),
        out.data_ptr(),
        x.shape[0],
        stream=torch.cuda.current_stream(),
    )
    return out
