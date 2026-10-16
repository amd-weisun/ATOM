# SPDX-License-Identifier: MIT
# Copyright (C) 2026, Advanced Micro Devices, Inc. All rights reserved.
"""Device helpers shared by every stage of the Qwen3.5 MonoKernel."""

import flydsl.expr as fx
from aiter.ops.flydsl.kernels import buffer_ops as bo
from flydsl.expr import const_expr, gpu, range_constexpr, rocdl
from flydsl.expr import math as fmath
from flydsl.expr.typing import T, as_ir_value

from atom.models.qwen3_5.mono.config import FP8_MAX
from atom.models.qwen3_5.mono.kernels.helpers import (
    SHARED_SOURCE_KEY,
    LaunchState,
    traced,
)
from atom.models.qwen3_5.mono.kernels.ops import (
    bf2_f32,
    bf16_pair,
    exp,
    lds_ld,
    lds_st,
    rcp,
    read_lane_i32,
    rsq,
    rsrc,
    wave_max,
    wave_umax,
    write_lane_i32,
    xshfl,
)
from atom.models.qwen3_5.mono.layout import MOE_MISC


def sort_network(n):
    """Odd-even transposition compare-exchange pairs for ``n`` elements."""
    pairs = []
    for r in range(n):
        for i in range(r % 2, n - 1, 2):
            pairs.append((i, i + 1))
    return pairs


def silu(x):
    """``x / (1 + exp(-x))``: the IEEE divide of aiter's and ATOM's SiLU kernels."""
    return x / (1.0 + exp(-x))


def sigmoid(x):
    return 1.0 / (1.0 + exp(-x))


def sigmoid_precise(x):
    """``1 / (1 + expf(-x))`` with the accurate ``expf`` (aiter's top-k kernel, torch's sigmoid)."""
    return 1.0 / (1.0 + fmath.exp(-x))


def sigmoid_fast(x):
    """``1.0f / (1.0f + expf(-x))`` of aiter's ``-ffast-math`` kernels: the reciprocal by ``v_rcp_f32``."""
    return rcp(1.0 + exp(-x))


def mxfp4_qd(v0, v1):
    """Per-32 MXFP4 quantize-dequantize of the pair (v0, v1) a lane holds, a 32-element block on 16
    consecutive lanes, as aiter's decode MoE quantizes (``mxfp4_moe_sort_quant_fwd`` and its stage-1
    epilogue, RCEIL): the E8M0 scale ``ceil_pow2(amax * fl(1 / 6))``, E2M1 round to nearest even, back
    to f32 (exact in bf16).
    """
    amax = fx.max(fmath.absf(v0), fmath.absf(v1))
    for o in (1, 2, 4, 8):
        amax = fx.max(amax, xshfl(amax, o))
    u = (amax * (1.0 / 6.0)).bitcast(fx.Int32)
    ef = (u >> 23) & 0xFF
    ef = ((ef < 0xFF) & ((u & 0x7FFFFF) != 0)).select(ef + 1, ef)
    e = ef - 127  # an all-zero block: 2^-127
    one = as_ir_value(fx.Float32(1.0))
    word = fx.Int32(
        rocdl.cvt_scalef32_pk_fp4_f32(
            T.i32,
            as_ir_value(fx.Int32(0)),
            as_ir_value(v0 * ((127 - e) << 23).bitcast(fx.Float32)),
            as_ir_value(v1 * ((127 - e) << 23).bitcast(fx.Float32)),
            one,
            0,
        )
    )
    out = fx.Vector(
        rocdl.cvt_scalef32_pk_f32_fp4(
            res=fx.Vector.make_type(2, fx.Float32),
            src=as_ir_value(word),
            scale=one,
            src_sel_index=0,
        )
    )
    ds = ((e + 127) << 23).bitcast(fx.Float32)
    return out[0] * ds, out[1] * ds


# The per-1x128 FP8 activation quants of baseline ATOM's decode, one per kernel family: rule(amax) ->
# (stored fp32 scale, x -> the f32 value converted to E4M3FN by v_cvt_pk_fp8_f32).


def _clamp8(x):
    return fx.min(fx.max(x, -FP8_MAX), FP8_MAX)


def quant_rule_q1(amax):
    """``dynamic_per_group_scaled_quant_kernel`` (MoE input, o_proj input): the block max at least 1e-10,
    the scale ``amax * fl(1 / 448)``, x times ``1 / scale`` (an IEEE divide), clamped.
    """
    sc = fx.max(amax, fx.Float32(1.0e-10)) * (1.0 / FP8_MAX)
    inv = 1.0 / sc
    return sc, lambda x: _clamp8(x * inv)


def quant_rule_q1_norm(amax):
    """``fused_qk_rmsnorm_group_quant`` (the first layer's input norm): Q1's scale, its reciprocal by
    ``v_rcp_f32``."""
    sc = fx.max(amax, fx.Float32(1.0e-10)) * (1.0 / FP8_MAX)
    inv = rcp(sc)
    return sc, lambda x: _clamp8(x * inv)


def quant_rule_q2(amax):
    """The fused all-reduce + RMSNorm + group quant (every later layer's input norm): ``amax / 448`` (1
    for an all-zero block), x divided by it, no clamp."""
    sc = (amax == 0.0).select(fx.Float32(1.0), amax / FP8_MAX)
    return sc, lambda x: x / sc


def quant_rule_q3(amax):
    """``gated_rmsnorm_fp8_group_quant_kernel`` (GDN out_proj input): ``amax / 448``, or 1e-10 for a block
    max <= 1e-10, x times ``1 / scale``, clamped."""
    sc = (amax > 1.0e-10).select(amax / FP8_MAX, fx.Float32(1.0e-10))
    inv = 1.0 / sc
    return sc, lambda x: _clamp8(x * inv)


def quant_rule_q4(amax):
    """ATOM's fused sigmoid * mul + quant (o_proj input under QKF): the block max at least 1e-10, divided
    by 448, x times ``1 / scale``, clamped."""
    sc = fx.max(amax, fx.Float32(1.0e-10)) / FP8_MAX
    inv = 1.0 / sc
    return sc, lambda x: _clamp8(x * inv)


def fp8_word(q0, q1, q2, q3):
    """Four f32 values (already scaled) as four E4M3FN bytes, K order."""
    lo = fx.Int32(rocdl.cvt_pk_fp8_f32(T.i32, q0, q1, fx.Int32(0), False))
    return fx.Int32(rocdl.cvt_pk_fp8_f32(T.i32, q2, q3, lo, True))


def quant_floor(a0, a1):
    """``quant_rule_q1`` of a 128-block held as 2 f32 per lane of a wave -> (q0, q1, scale)."""
    sc, q = quant_rule_q1(wave_max(fx.max(fmath.absf(a0), fmath.absf(a1))))
    return q(a0), q(a1), sc


@traced
def common_defs(ctx):
    """The shared helpers bound to this launch, plus the Qwen-specific ones."""
    EPS = ctx["EPS"]
    G = ctx["G"]
    HIDDEN = ctx["HIDDEN"]
    ID_BITS = ctx["ID_BITS"]
    ID_MASK = ctx["ID_MASK"]
    N_EXPERTS = ctx["N_EXPERTS"]
    ROUTER_ROWS = ctx["ROUTER_ROWS"]
    S = ctx["S"]
    SC = ctx["SC"]
    SY = ctx["SY"]
    TOP_K = ctx["TOP_K"]
    W = ctx["W"]
    XQ_BLOCKS = ctx["XQ_BLOCKS"]
    base = ctx["base"]
    bid = ctx["bid"]
    lane = ctx["lane"]
    misc = ctx["misc"]
    outs = ctx["outs"]
    peer_dst = ctx["peer_dst"]
    rank = ctx["rank"]
    red = ctx["red"]
    scratch = ctx["scratch"]
    sym = ctx["sym"]
    tag = ctx["tag"]
    tid = ctx["tid"]
    wave = ctx["wave"]
    xs = ctx["xs"]

    st = LaunchState(
        source_key=SHARED_SOURCE_KEY,
        S=S,
        tid=tid,
        lane=lane,
        wave=wave,
        bid=bid,
        G=G,
        base=base,
        scratch=scratch,
        SC=SC,
        tag=tag,
        red=red,
        outs=outs,
        xs=xs,
        rank=rank,
        W=W,
        HIDDEN=HIDDEN,
        peer_dst=peer_dst,
        sym=sym,
        SY=SY,
        eps=EPS,
    )
    mb = st.mb
    poll = st.poll
    block_sums = st.block_sums
    rmsnorm_tail_ks = st.rmsnorm_tail_ks

    SX_CHUNK = (
        4  # past 8 samples: the samples whose x a thread holds at once while staging
    )

    def put_x8(s, n, k, v4, fp8_rule):
        """FP8 X[s][k .. k + 3] (K order) of the f32 normed ``v4``; the 32 lanes of a 128-block share its
        scale, misc[MOE_MISC + s * XQ_BLOCKS + k / 128]."""
        amax = fx.max(
            fx.max(fmath.absf(v4[0]), fmath.absf(v4[1])),
            fx.max(fmath.absf(v4[2]), fmath.absf(v4[3])),
        )
        for o in (1, 2, 4, 8, 16):
            amax = fx.max(amax, xshfl(amax, o))
        sc, q = fp8_rule(amax)
        lds_st(
            xs,
            (s * n + k) // 4,
            fp8_word(q(v4[0]), q(v4[1]), q(v4[2]), q(v4[3])).bitcast(fx.Float32),
        )
        if lane % 32 == 0:
            lds_st(misc, MOE_MISC + s * XQ_BLOCKS + k // 128, sc)

    def stage_x_gemma(ld4s, n, gamma, count=None, fp8_rule=None):
        """LDS X[s][0:n] = rmsnorm(x_s) * gamma for ``count`` samples, ``gamma`` an f32 vector (the Gemma
        ``1 + w``, formed on the host); ld4s([(s, k)]) -> [(x_s[k], .., x_s[k+3])]. As bf16 pairs, or
        with ``fp8_rule`` (one of the ``quant_rule_*``) as FP8 bytes in K order quantized per 128-block
        from the f32 normed values, the block scales in misc[MOE_MISC + s * XQ_BLOCKS + block].
        """
        count = S if count is None else count
        ks, active = rmsnorm_tail_ks(n)
        per = len(ks)
        assert fp8_rule is None or (
            active is None and n % 128 == 0
        ), "FP8 staging takes whole 128-blocks"
        rg_ = rsrc(gamma)
        gs = []
        for k in ks:
            g = fx.Vector(bo.buffer_load(rg_, k, vec_width=4, dtype=T.f32))
            gs.append([g[j] for j in range(4)])

        def sumsq(vs, s):
            ss = fx.Float32(0.0)
            for i in range_constexpr(per):
                for a in vs[s * per + i]:
                    term = a * a
                    if const_expr(active is not None and i == per - 1):
                        term = active.select(term, fx.Float32(0.0))
                    ss = ss + term
            return ss

        if const_expr(count <= 8):
            vals = ld4s([(s, k) for s in range(count) for k in ks])
            sss = [sumsq(vals, s) for s in range(count)]
            rstds = [rsq(tot * (1.0 / n) + EPS) for tot in block_sums(sss)]
            for s in range_constexpr(count):
                for i in range_constexpr(per):
                    a = vals[s * per + i]
                    if const_expr(fp8_rule is not None):
                        put_x8(
                            s,
                            n,
                            ks[i],
                            [a[e] * rstds[s] * gs[i][e] for e in range(4)],
                            fp8_rule,
                        )
                        continue
                    for j in range_constexpr(2):
                        lds_st(
                            xs,
                            (s * n + ks[i]) // 2 + j,
                            bf16_pair(
                                a[2 * j] * rstds[s] * gs[i][2 * j],
                                a[2 * j + 1] * rstds[s] * gs[i][2 * j + 1],
                            ),
                        )
            return rstds
        # more samples than registers hold at once: chunks of SX_CHUNK park their raw bf16 x in LDS
        # (lossless: x is bf16) while their sums of squares accumulate; one block reduction; then each
        # thread normalizes its own words in place
        sss = []
        for c0 in range_constexpr(0, count, SX_CHUNK):
            cn = min(SX_CHUNK, count - c0)
            if const_expr(c0 > 0):
                rocdl.sched_barrier(0)  # one chunk's loads in flight at a time
            vals = ld4s([(c0 + s, k) for s in range(cn) for k in ks])
            for s in range_constexpr(cn):
                sss.append(sumsq(vals, s))
                for i in range_constexpr(per):
                    a = vals[s * per + i]
                    for j in range_constexpr(2):
                        lds_st(
                            xs,
                            ((c0 + s) * n + ks[i]) // 2 + j,
                            bf16_pair(a[2 * j], a[2 * j + 1]),
                        )
        rstds = [rsq(tot * (1.0 / n) + EPS) for tot in block_sums(sss)]
        if const_expr(fp8_rule is not None):
            # sample s's FP8 bytes land on the parked x of samples <= s / 2: every thread reads its
            # sample-s words before any writes them
            for s in range_constexpr(count):
                raw = []
                for i in range_constexpr(per):
                    for j in range_constexpr(2):
                        raw += list(
                            bf2_f32(
                                lds_ld(xs, (s * n + ks[i]) // 2 + j).bitcast(fx.Int32)
                            )
                        )
                gpu.barrier()
                for i in range_constexpr(per):
                    put_x8(
                        s,
                        n,
                        ks[i],
                        [raw[4 * i + e] * rstds[s] * gs[i][e] for e in range(4)],
                        fp8_rule,
                    )
            return rstds
        for s in range_constexpr(count):
            for i in range_constexpr(per):
                for j in range_constexpr(2):
                    w = (s * n + ks[i]) // 2 + j
                    a0, a1 = bf2_f32(lds_ld(xs, w).bitcast(fx.Int32))
                    lds_st(
                        xs,
                        w,
                        bf16_pair(
                            a0 * rstds[s] * gs[i][2 * j],
                            a1 * rstds[s] * gs[i][2 * j + 1],
                        ),
                    )
        return rstds

    def stage_x8(q_name, s_name, n, samples):
        """LDS FP8 rows [samples, n] (K order) from mailbox ``q_name`` (four bytes a word) and their
        per-128 scales from ``s_name``, to misc[MOE_MISC + j * (n / 128) + block]."""
        nq = n // 4
        nb = n // 128
        full = nq // 512
        xk = [tid + i * 512 for i in range(full)]
        if const_expr(nq % 512):
            xk.append(fx.min(tid + full * 512, nq - 1))
        nxw = len(xk)
        got = poll(
            [(mb(q_name), sx * nq + k, 1) for sx in samples for k in xk]
            + [(mb(s_name), sx * nb + fx.min(tid, nb - 1), 1) for sx in samples]
        )
        for j in range_constexpr(len(samples)):
            for i in range_constexpr(nxw):
                lds_st(xs, j * nq + xk[i], got[j * nxw + i][0].bitcast(fx.Float32))
            if tid < nb:
                lds_st(
                    misc,
                    MOE_MISC + j * nb + tid,
                    got[len(samples) * nxw + j][0].bitcast(fx.Float32),
                )

    def stage_moe_input(samples):
        """LDS FP8 MoE inputs (K order) and their block scales (misc[MOE_MISC + ...])."""
        stage_x8("xq", "xqs", HIDDEN, samples)

    def stage_f32_q8(name, n, fp8_rule):
        """``stage_pairs_q8`` of the f32 mailbox ``name`` (one value a pair)."""
        nq = S * n // 4
        for i in range_constexpr(-(-nq // 512)):
            w = tid + i * 512
            if w < nq:
                v = poll([(mb(name), w * 4, 2), (mb(name), w * 4 + 2, 2)])
                a = [v[j // 2][j % 2].bitcast(fx.Float32) for j in range(4)]
                amax = fx.max(
                    fx.max(fmath.absf(a[0]), fmath.absf(a[1])),
                    fx.max(fmath.absf(a[2]), fmath.absf(a[3])),
                )
                for o in (1, 2, 4, 8, 16):
                    amax = fx.max(amax, xshfl(amax, o))
                sc, q = fp8_rule(amax)
                lds_st(xs, w, fp8_word(*[q(x) for x in a]).bitcast(fx.Float32))
                if lane % 32 == 0:
                    lds_st(misc, MOE_MISC + w // 32, sc)

    def stage_pairs_q8(name, n, fp8_rule):
        """LDS FP8 [S, n] (K order) of the bf16 mailbox ``name``, quantized per 128-block by ``fp8_rule``
        (32 consecutive threads hold a block, 4 values each); scales to misc[MOE_MISC + s * (n / 128) +
        block]."""
        nq = S * n // 4
        for i in range_constexpr(-(-nq // 512)):
            w = tid + i * 512
            if w < nq:  # whole blocks: n % 128 == 0 keeps a block's 32 threads together
                v = poll([(mb(name), w * 2, 2)])[0]
                a0, a1 = bf2_f32(v[0].bitcast(fx.Int32))
                a2, a3 = bf2_f32(v[1].bitcast(fx.Int32))
                amax = fx.max(
                    fx.max(fmath.absf(a0), fmath.absf(a1)),
                    fx.max(fmath.absf(a2), fmath.absf(a3)),
                )
                for o in (1, 2, 4, 8, 16):
                    amax = fx.max(amax, xshfl(amax, o))
                sc, q = fp8_rule(amax)
                lds_st(xs, w, fp8_word(q(a0), q(a1), q(a2), q(a3)).bitcast(fx.Float32))
                if lane % 32 == 0:
                    lds_st(misc, MOE_MISC + w // 32, sc)

    def route_softmax(s):
        """Softmax top-``TOP_K`` of sample ``s``'s router logits (one whole wave), as aiter's
        ``topkGatingSoftmax``: the picks on the raw (bf16) logits in descending order, ties to the
        lower expert id; ``expf(l - l_max)`` of each pick, their sum in pick order, each times
        ``1 / sum``. Returns (expert id, weight) of pick ``lane``, valid in lanes < TOP_K.
        """
        KPL = N_EXPERTS // 64
        raws = st.getf_many(
            [(mb("scores"), s * ROUTER_ROWS + lane + i * 64) for i in range(KPL)]
        )
        ks = []
        for i in range_constexpr(KPL):
            # -0.0 ties +0.0 (a float compare), so it keys as +0.0
            kb = (raws[i] == 0.0).select(fx.Int32(0), raws[i].bitcast(fx.Int32))
            ok = (kb >= 0).select(kb ^ fx.Int32(-(2**31)), ~kb)
            ks.append(
                fx.Uint32(
                    (ok & fx.Int32(-(1 << ID_BITS))) | (ID_MASK - (lane + i * 64))
                )
            )
        for a, b in sort_network(KPL):
            ks[a], ks[b] = fx.max(ks[a], ks[b]), fx.min(ks[a], ks[b])
        ks = [fx.Int32(k) for k in ks] + [fx.Int32(0)]
        mv = fx.Int32(0)  # lane k: the key of pick k
        for k in range_constexpr(TOP_K):
            m = wave_umax(ks[0])
            hit = ks[0] == m
            ks = [hit.select(ks[i + 1], ks[i]) for i in range(KPL)] + [ks[KPL]]
            mv = write_lane_i32(m, k, mv)
        e = ID_MASK - (mv & ID_MASK)
        logit = st.getf(mb("scores"), s * ROUTER_ROWS + fx.min(e, N_EXPERTS - 1))
        top = read_lane_i32(logit.bitcast(fx.Int32), 0).bitcast(fx.Float32)
        p = (lane < TOP_K).select(fmath.exp(logit - top), fx.Float32(0.0))
        tot = fx.Float32(0.0)
        for k in range_constexpr(TOP_K):
            tot = tot + read_lane_i32(p.bitcast(fx.Int32), k).bitcast(fx.Float32)
        return e, p * (1.0 / tot)

    return {
        "st": st,
        "mb": mb,
        "poll": poll,
        "put": st.put,
        "put2": st.put2,
        "put_bf": st.put_bf,
        "get": st.get,
        "getf": st.getf,
        "getf_many": st.getf_many,
        "get2_many": st.get2_many,
        "block_sum": st.block_sum,
        "block_sums": block_sums,
        "unit_fp8": st.unit_fp8,
        "unit_bf16": st.unit_bf16,
        "unit_f8f8": st.unit_f8f8,
        "mma_units": st.mma_units,
        "run_units": st.run_units,
        "reduce_rows": st.reduce_rows,
        "emit_out": st.emit_out,
        "n_sel": st.n_sel,
        "stage_x_pairs": st.stage_x_pairs,
        "quant_scaled": st.quant_scaled,
        "st_f8": st.st_f8,
        "peer_reduce": st.peer_reduce,
        "start": st.start,
        "stage_x_gemma": stage_x_gemma,
        "stage_moe_input": stage_moe_input,
        "stage_x8": stage_x8,
        "stage_pairs_q8": stage_pairs_q8,
        "stage_f32_q8": stage_f32_q8,
        "route_softmax": route_softmax,
    }
