# SPDX-License-Identifier: MIT
# Copyright (C) 2026, Advanced Micro Devices, Inc. All rights reserved.
# ruff: noqa: B023  (traced stage closures read the loop's values on purpose: the tracer unrolls)
"""out_proj of the Qwen3.8 MonoKernel's token mixer and the attention-side hyper-connection combine:
``x1_s = X_s + bf16(y) * 2 sigmoid(r_s / hc)`` per stream -> mailbox ``x1`` (bf16 pairs).
"""

import flydsl.expr as fx
from aiter.ops.flydsl.kernels import buffer_ops as bo
from flydsl.expr import gpu
from flydsl.expr import math as fmath
from flydsl.expr.typing import T

from atom.models.qwen3_5.mono.kernels.common import sigmoid
from atom.models.qwen3_5.mono.kernels.helpers import traced
from atom.models.qwen3_5.mono.kernels.ops import bf2_f32, bf16_round, lds_ld, rsrc
from atom.models.qwen4_exp.mono.layout import OUT_TILE, WAVES


@traced
def out_stage(ctx):
    G = ctx["G"]
    HC = ctx["HC"]
    HIDDEN = ctx["HIDDEN"]
    OUT_K = ctx["OUT_K"]
    S = ctx["S"]
    emit_out = ctx["emit_out"]
    h_in = ctx["h_in"]
    mb = ctx["mb"]
    n_sel = ctx["n_sel"]
    outs = ctx["outs"]
    poll = ctx["poll"]
    put_bf = ctx["put_bf"]
    reduce_rows = ctx["reduce_rows"]
    run_units = ctx["run_units"]
    stage_x_pairs = ctx["stage_x_pairs"]
    start = ctx["start"]
    tid = ctx["tid"]
    unit_bf16_rows = ctx["unit_bf16_rows"]
    w_out = ctx["w_out"]
    wave = ctx["wave"]
    NKC = OUT_K // 64
    assert NKC % WAVES == 0 and OUT_TILE == 16
    UPW = NKC // WAVES
    W4 = HC * HIDDEN
    r_wo, r_h = rsrc(w_out), rsrc(h_in)
    for t in range(start("out"), HIDDEN // OUT_TILE, G):
        t = fx.Int32(t)

        def u_o(c):
            kc = wave * UPW + c
            return unit_bf16_rows(r_wo, t, kc, OUT_K, (n_sel() * OUT_K + kc * 64) // 2)

        pre = [u_o(c) for c in range(UPW)]
        gpu.barrier()
        stage_x_pairs("gated", S * OUT_K, lambda k: k)
        gpu.barrier()
        acc = run_units(u_o, UPW, UPW, pre)
        reduce_rows(1, acc, emit_out(OUT_TILE))
        gpu.barrier()
        # thread (sample n, stream st, row pair): the combine into the bundle
        if tid < S * HC * OUT_TILE // 2:
            n = tid // (HC * OUT_TILE // 2)
            st = (tid // (OUT_TILE // 2)) % HC
            rp = (tid % (OUT_TILE // 2)) * 2
            r = poll([(mb("hr_a"), n * HC + st, 1)])[0][0].bitcast(fx.Float32)
            inj = 2.0 * sigmoid(bf16_round(r * (1.0 / HC)))
            idx = n * W4 + st * HIDDEN + t * OUT_TILE + rp
            x0, x1 = bf2_f32(
                fx.Int32(bo.buffer_load(r_h, idx // 2, vec_width=1, dtype=T.i32))
            )
            y0 = bf16_round(lds_ld(outs, n * OUT_TILE + rp))
            y1 = bf16_round(lds_ld(outs, n * OUT_TILE + rp + 1))
            # ATOM: x + y * inj, fused
            put_bf(mb("x1"), idx, [fmath.fma(y0, inj, x0), fmath.fma(y1, inj, x1)])
    return {}
