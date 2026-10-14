# SPDX-License-Identifier: MIT
# Copyright (C) 2026, Advanced Micro Devices, Inc. All rights reserved.
# ruff: noqa: B023  (traced stage closures read the loop's values on purpose: the tracer unrolls)
"""What both token mixers of the Qwen3.5 MonoKernel share: the input-RMSNorm projection GEMV and
out_proj with the attention-side TP reduce."""

import flydsl.expr as fx
from aiter.ops.flydsl.kernels import buffer_ops as bo
from flydsl.expr import const_expr, gpu
from flydsl.expr.typing import T

from atom.models.qwen3_5.mono.kernels.helpers import traced
from atom.models.qwen3_5.mono.kernels.ops import bf2_f32, lds_ld, rsrc
from atom.models.qwen3_5.mono.layout import PROJ_GROUPS, PROJ_TILE, ROW_TILE, WAVES


@traced
def mixer_defs(ctx):
    """``proj_stage``: a GEMV over ``rmsnorm(h) * (1 + g_in)`` whose rows go to an epilogue."""
    G = ctx["G"]
    HIDDEN = ctx["HIDDEN"]
    MX = ctx["MX"]
    S = ctx["S"]
    emit_out = ctx["emit_out"]
    g_in1 = ctx["g_in1"]
    h_in = ctx["h_in"]
    n_sel = ctx["n_sel"]
    outs = ctx["outs"]
    reduce_rows = ctx["reduce_rows"]
    run_units = ctx["run_units"]
    stage_x_gemma = ctx["stage_x_gemma"]
    start = ctx["start"]
    tid = ctx["tid"]
    unit_bf16 = ctx["unit_bf16"]
    unit_bf16_rows = ctx["st"].unit_bf16_rows
    unit_fp8 = ctx["unit_fp8"]
    wave = ctx["wave"]
    r_h = rsrc(h_in)
    NKC = HIDDEN // 64
    WPR = WAVES // PROJ_GROUPS
    UPW = NKC // WPR
    ROWS = PROJ_TILE * PROJ_GROUPS

    def ld_h(sks):
        res = []
        for s, k in sks:
            w = fx.Vector(
                bo.buffer_load(r_h, (s * HIDDEN + k) // 2, vec_width=2, dtype=T.i32)
            )
            v = w.bitcast(fx.BFloat16).to(fx.Float32)
            res.append([v[j] for j in range(4)])
        return res

    def proj_stage(name, n_groups, w, sc, bf16_w, epilogue):
        """Stage ``name``: ``n_groups`` 16-row groups of FP8 (scales ``sc``) or BF16 matrix ``w``,
        PROJ_GROUPS per task; ``epilogue(s, row, value)`` takes each output (``tid`` < S * rows). An FP8
        matrix of the MXFP4 checkpoint (``MX``) is BF16 there, row-major as ATOM holds it.
        """
        r_w, r_s = rsrc(w), (None if bf16_w else rsrc(sc))
        batch = max(1, NKC // WAVES // (4 if bf16_w else 1))  # units in flight per wave
        if const_expr(S > 8):  # past 8 samples the staged activations need the room
            batch = max(1, batch // 2)
        for t in range(start(name), n_groups // PROJ_GROUPS, G):
            t = fx.Int32(t)
            gpu.barrier()
            rg = t * PROJ_GROUPS + wave // WPR

            def u(c):
                kc = (wave % WPR) * UPW + c
                bw = (n_sel() * HIDDEN + kc * 64) // 2
                if const_expr(bf16_w):
                    return unit_bf16(r_w, rg, kc, NKC, bw)
                if const_expr(MX):
                    return unit_bf16_rows(r_w, rg, kc, HIDDEN, bw)
                return unit_fp8(r_w, r_s, rg, kc, NKC, HIDDEN, 128, bw, shuffled=True)

            pre = [u(c) for c in range(batch)]
            stage_x_gemma(ld_h, HIDDEN, g_in1)
            gpu.barrier()
            acc = run_units(u, UPW, batch, pre)
            reduce_rows(PROJ_GROUPS, acc, emit_out(ROWS))
            gpu.barrier()
            if tid < S * ROWS:
                epilogue(tid // ROWS, t * ROWS + tid % ROWS, lds_ld(outs, tid))

    return {"proj_stage": proj_stage}


@traced
def out_stages(ctx):
    """out_proj over the ``gated`` mailbox (the mixer's [S, OUT_K] bf16 output), the attention-side TP
    peer reduce and the residual ``h`` -> mailbox ``a``."""
    G = ctx["G"]
    HIDDEN = ctx["HIDDEN"]
    MX = ctx["MX"]
    OUT_K = ctx["OUT_K"]
    S = ctx["S"]
    emit_out = ctx["emit_out"]
    h_in = ctx["h_in"]
    mb = ctx["mb"]
    n_sel = ctx["n_sel"]
    peer_reduce = ctx["peer_reduce"]
    put_bf = ctx["put_bf"]
    reduce_rows = ctx["reduce_rows"]
    run_units = ctx["run_units"]
    s_out = ctx["s_out"]
    stage_x_pairs = ctx["stage_x_pairs"]
    start = ctx["start"]
    unit_fp8 = ctx["unit_fp8"]
    w_out = ctx["w_out"]
    wave = ctx["wave"]
    r_h = rsrc(h_in)
    OUT_NKC = OUT_K // 64
    OUT_R = ROW_TILE // 16
    OUT_WPR = WAVES // OUT_R
    OUT_UPW = OUT_NKC // OUT_WPR
    r_wo, r_so = rsrc(w_out), rsrc(s_out)

    def residual(s, row):
        return bf2_f32(
            fx.Int32(
                bo.buffer_load(r_h, (s * HIDDEN + row) // 2, vec_width=1, dtype=T.i32)
            )
        )

    for t in range(start("out"), HIDDEN // ROW_TILE, G):
        t = fx.Int32(t)
        rg = t * OUT_R + wave // OUT_WPR

        def u_o(c):
            kc = (wave % OUT_WPR) * OUT_UPW + c
            bw = (n_sel() * OUT_K + kc * 64) // 2
            if const_expr(MX):
                return ctx["st"].unit_bf16_rows(r_wo, rg, kc, OUT_K, bw)
            return unit_fp8(r_wo, r_so, rg, kc, OUT_NKC, OUT_K, 128, bw, shuffled=True)

        pre = [u_o(c) for c in range(OUT_UPW)]
        gpu.barrier()
        stage_x_pairs("gated", S * OUT_K, lambda k: k)
        gpu.barrier()
        acc = run_units(u_o, OUT_UPW, OUT_UPW, pre)
        reduce_rows(OUT_R, acc, emit_out(ROW_TILE))
        gpu.barrier()
        peer_reduce(
            "attn",
            t,
            residual,
            lambda s, row, v0, v1: put_bf(mb("a"), s * HIDDEN + row, [v0, v1]),
            tile=ROW_TILE,
        )
        gpu.barrier()
    return {}
