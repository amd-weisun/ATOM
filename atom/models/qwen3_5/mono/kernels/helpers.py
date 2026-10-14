# SPDX-License-Identifier: MIT
# Copyright (C) 2026, Advanced Micro Devices, Inc. All rights reserved.
"""Device helpers of the Qwen3.5 mono layer kernel.

Every helper is a plain function whose first argument is the kernel's ``LaunchState``: the values of
one launch (``tag``, ``lane`` / ``wave`` / ``tid``, scratch layout, LDS pointers, task bases, ...).
``LaunchState`` also exposes each helper bound to it (``st.put``, ``st.poll``, ...), and helpers call
one another through it, so a kernel that installs its own variant (``st.poll = partial(poll, st,
retry_spin_pause=True)``, or a function of its own) changes it for every helper. Variants are
keyword arguments (``poll``, ``peer_reduce``) or separate functions, each a build-time choice.
"""

from functools import partial

import flydsl.expr as fx
from aiter.ops.flydsl.kernels import buffer_ops as bo
from flydsl.compiler.ast_rewriter import ASTRewriter
from flydsl.expr import const_expr, gpu, range_constexpr, rocdl
from flydsl.expr import math as fmath
from flydsl.expr.typing import T

from atom.models.qwen3_5.mono.kernels.ops import (
    CM_DEV,
    CM_SYS,
    EPS,
    FP8_MAX,
    POLL_MAX,
    ROW_TILE,
    SCALE_BM,
    THREADS,
    WAVES,
    bf2_f32,
    bf16_pair,
    f8_word,
    fp8_to_bf16x8,
    ld_f32,
    lds_ld,
    lds_st,
    mem_realtime,
    mxfp4_to_bf16x8,
    rcp,
    rsrc,
    spin_pause,
    uniform,
    wave_max,
    wave_sum,
    xshfl,
)
from atom.models.qwen3_5.mono.sources import SOURCES

# a kernel captures this digest of the model's kernel sources and the mono framework
# (``sources.SOURCES``): the JIT cache keys a helper's source only when its walk reaches it
SHARED_SOURCE_KEY = SOURCES

# Helpers with dynamic control flow need flydsl's AST rewriting (as a @flyc.kernel body gets), so that
# their ``if`` / ``while`` / ``for range`` over traced values lower to scf ops. ASTRewriter is not part of
# flydsl's stable API; this is the one place that depends on it.
traced = ASTRewriter.transform


# --------------------------------------------------------------------------- tagged-pair mailboxes


def mb(st, name):
    SC = st.SC
    scratch = st.scratch
    return scratch + fx.Int64(SC[name])


def put(st, base_addr, i, v, cm=CM_DEV):
    """Pair i := (v, tag); ``v`` f32 (or int32 bits)."""
    tag = st.tag
    bits = v.bitcast(fx.Int32) if isinstance(v, fx.Float32) else fx.Int32(v)
    bo.buffer_store(
        fx.Vector.from_elements([bits, tag], fx.Int32),
        rsrc(base_addr),
        i * 2,
        cache_modifier=cm,
    )


def put2(st, base_addr, i, v0, v1, cm=CM_DEV):
    """Pairs i, i+1 (i even) in one 16-byte store."""
    tag = st.tag
    vec = fx.Vector.from_elements(
        [fx.Float32(v0).bitcast(fx.Int32), tag, fx.Float32(v1).bitcast(fx.Int32), tag],
        fx.Int32,
    )
    bo.buffer_store(vec, rsrc(base_addr), i * 2, cache_modifier=cm)


def put_bf(st, base_addr, i, vs, cm=CM_DEV, store_tag=None):
    """Elements i .. i + len(vs) (2 or 4, i aligned) as packed bf16 pairs: pair
    i / 2 + j := (bf16(vs[2j]) | bf16(vs[2j + 1]) << 16, tag), one 8 / 16-byte store."""
    tag = st.tag
    write_tag = tag if store_tag is None else store_tag
    words = []
    for j in range_constexpr(len(vs) // 2):
        words += [bf16_pair(vs[2 * j], vs[2 * j + 1]).bitcast(fx.Int32), write_tag]
    bo.buffer_store(
        fx.Vector.from_elements(words, fx.Int32), rsrc(base_addr), i, cache_modifier=cm
    )


@traced
def poll(
    st,
    specs,
    scope="agent",
    batch=POLL_MAX,
    expected_tag=None,
    *,
    retry_spin_pause=False,
    hang=None,
    poll_timeout_ticks=None,
):
    """Batched poll of mailbox pairs ``specs`` = [(base_addr, pair index, npairs in {1, 2})].

    All pairs are loaded together with plain 8 / 16-byte coherent buffer loads (sc1 locally,
    sc0 sc1 for peer memory); while any tag is not ``expected_tag`` (default: this launch's)
    the whole batch is re-loaded, so a batch costs one round trip after its last producer
    lands. A side-effecting op in the retry loop keeps the loads from being hoisted. With
    ``poll_timeout_ticks`` a timed-out poll stores the tag into ``hang``, and later polls
    seeing it give up. Returns one list of Int32 value bits per spec."""
    tag = st.tag
    poll = st.poll
    if const_expr(len(specs) == 0):
        return []
    if const_expr(len(specs) > batch):  # bound live registers
        return poll(specs[:batch], scope, batch, expected_tag) + poll(
            specs[batch:], scope, batch, expected_tag
        )
    cm = CM_DEV if const_expr(scope == "agent") else CM_SYS
    wanted_tag = tag if expected_tag is None else expected_tag

    def load_all():
        words = []
        for b, i, n in specs:
            w = fx.Vector(
                bo.buffer_load(
                    rsrc(b),
                    fx.Int32(i) * 2,
                    vec_width=2 * n,
                    dtype=T.i32,
                    cache_modifier=cm,
                )
            )
            words += [w[e] for e in range(2 * n)]
        return fx.Vector.from_elements(words, fx.Int32)

    nw = sum(2 * n for _, _, n in specs)

    def unpack(v):
        outs_, e = [], 0
        for _, _, n in specs:
            outs_.append([v[e + 2 * q] for q in range(n)])
            e += 2 * n
        return outs_

    def pending(v):
        bad = v[1] != wanted_tag
        for e in range_constexpr(3, nw, 2):
            bad = bad | (v[e] != wanted_tag)
        return bad

    v = load_all()
    if const_expr(poll_timeout_ticks is not None):
        t0 = mem_realtime()
        stop = fx.Int32(0)
        while pending(v) & (stop == 0):
            rocdl.s_nop(0)
            v = load_all()
            flagged = (
                uniform(
                    bo.buffer_load(
                        rsrc(hang), 0, vec_width=1, dtype=T.i32, cache_modifier=CM_DEV
                    )
                )
                == tag
            )
            late = (mem_realtime() - t0) > fx.Int64(poll_timeout_ticks)
            stop = late.select(fx.Int32(2), flagged.select(fx.Int32(1), fx.Int32(0)))
        if stop == 2:
            bo.buffer_store(tag, rsrc(hang), 0, cache_modifier=CM_DEV)
    else:
        while pending(v):
            if const_expr(retry_spin_pause):
                spin_pause()
            else:
                rocdl.s_nop(0)
            v = load_all()
    return unpack(v)


def get(st, base_addr, i):
    poll = st.poll
    return poll([(base_addr, i, 1)])[0][0]


def getf(st, base_addr, i):
    get = st.get
    return get(base_addr, i).bitcast(fx.Float32)


def getf_many(st, specs):
    """[(base, i)] single pairs -> list of f32."""
    poll = st.poll
    return [v[0].bitcast(fx.Float32) for v in poll([(b, i, 1) for b, i in specs])]


def get2_many(st, specs):
    """[(base, i)] double pairs (i even) -> list of (f32, f32)."""
    poll = st.poll
    return [
        (v[0].bitcast(fx.Float32), v[1].bitcast(fx.Float32))
        for v in poll([(b, i, 2) for b, i in specs])
    ]


# --------------------------------------------------------------------- task placement


def start(st, name):
    G = st.G
    base = st.base
    bid = st.bid
    return (bid + (G - base[name])) & (G - 1)


# -------------------------------------------------------------------------------- block reductions


@traced
def block_sums(st, vs):
    """Block-wide sums of several per-thread values with one LDS exchange."""
    lane = st.lane
    red = st.red
    wave = st.wave
    ws = [wave_sum(v) for v in vs]
    if lane == 0:
        for i in range_constexpr(len(vs)):
            lds_st(red, i * WAVES + wave, ws[i])
    gpu.barrier()
    tots = []
    for i in range_constexpr(len(vs)):
        t = lds_ld(red, i * WAVES)
        for w in range_constexpr(1, WAVES):
            t = t + lds_ld(red, i * WAVES + w)
        tots.append(t)
    gpu.barrier()
    return tots


@traced
def block_sum(st, v):
    lane = st.lane
    red = st.red
    wave = st.wave
    w = wave_sum(v)
    if lane == 0:
        lds_st(red, wave, w)
    gpu.barrier()
    t = lds_ld(red, 0)
    for i in range_constexpr(1, WAVES):
        t = t + lds_ld(red, i)
    gpu.barrier()
    return t


# --------------------------------------------------------------------------------- MFMA GEMV units


def unit_fp8(
    st, w_rsrc, s_rsrc, rg, kc, NKC, K, BK, b_word, coef=None, ln=None, shuffled=False
):
    """Issue one 64-k chunk of row group ``rg`` of a packed FP8 matrix; the
    bf16 activation chunk starts at LDS word ``b_word``. ``shuffled``: the matrix is in
    aiter's ``shuffle_weight`` (16, 16) layout (lane ``16 * kq + r`` holds K 16 * kq ..
    16 * kq + 15 of row r) instead of ``pack_fp8``'s."""
    lane = st.lane
    ln = lane if ln is None else ln
    wv = fx.Vector(
        bo.buffer_load(
            w_rsrc, ((rg * NKC + kc) * 64 + ln) * 4, vec_width=4, dtype=T.i32
        )
    )
    s = ld_f32(s_rsrc, (rg * 16 // SCALE_BM) * (K // BK) + kc * 64 // BK)
    fmt, bw = (
        ("fp8s", b_word + (lane // 16) * 8)
        if shuffled
        else ("fp8", b_word + (lane // 16) * 4)
    )
    if const_expr(callable(coef)):  # factor known only after a later wait
        return (fmt, [wv], lambda: s * coef(), bw)
    if const_expr(coef is not None):
        s = s * coef
    return (fmt, [wv], s, bw)


def unit_f8f8(st, w_rsrc, s_rsrc, rg, kc, NKC, K, b_word, coef, ln=None):
    """Issue one 128-k chunk (64-k chunks kc, kc + 1) of row group ``rg`` against the
    FP8 activation at LDS word ``b_word``; ``coef()`` = activation scale (x route weight).
    """
    lane = st.lane
    ln = lane if ln is None else ln
    wv = [
        fx.Vector(
            bo.buffer_load(
                w_rsrc, ((rg * NKC + kc + h) * 64 + ln) * 4, vec_width=4, dtype=T.i32
            )
        )
        for h in range(2)
    ]
    s = ld_f32(s_rsrc, (rg * 16 // SCALE_BM) * (K // 128) + kc // 2)
    return ("f8f8", wv, lambda: s * coef(), b_word + (lane // 16) * 4)


def unit_bf16_rows(st, w_rsrc, rg, kc, K, b_word, ln=None):
    """Issue one 64-k chunk of row group ``rg`` of a row-major BF16 ``[N, K]`` matrix (ATOM's unshuffled
    layout): lane ``16 * g + r`` reads K 64 kc + 16 g .. + 15 of row r (32 contiguous bytes), the activation
    order of the ``fp8s`` units."""
    lane = st.lane
    ln = lane if ln is None else ln
    base = ((rg * 16 + ln % 16) * K + kc * 64 + (ln // 16) * 16) // 2
    wv = [
        fx.Vector(bo.buffer_load(w_rsrc, base + 4 * h, vec_width=4, dtype=T.i32))
        for h in range(2)
    ]
    return ("bf16s", wv, None, b_word + (lane // 16) * 8)


def unit_bf16(st, w_rsrc, rg, kc, NKC, b_word, ln=None):
    lane = st.lane
    ln = lane if ln is None else ln
    wv = [
        fx.Vector(
            bo.buffer_load(
                w_rsrc,
                (((rg * NKC + kc) * 2 + sp) * 64 + ln) * 4,
                vec_width=4,
                dtype=T.i32,
            )
        )
        for sp in range(2)
    ]
    return ("bf16", wv, None, b_word + (lane // 16) * 4)


def unit_mxfp4_atom(st, w_rsrc, s_rsrc, rg, kc, K, b_word, coef=None, ln=None):
    """Issue one 128-K tile of an MXFP4 bank in ATOM's gfx950 layout and its E8M0 scale (lane
    ``16 * kl + r`` holds one 32-K scale block of row ``r``; the scale dword's four bytes cover
    tiles kc (even, odd) x row groups (even, odd))."""
    lane = st.lane
    ln = lane if ln is None else ln
    k1n = -(-(K // 32) // 8)  # scale K blocks, padded to 8, in groups of 8
    raw = fx.Vector(
        bo.buffer_load(
            w_rsrc, ((rg * (K // 128) + kc) * 64 + ln) * 4, vec_width=4, dtype=T.i32
        )
    )
    word = fx.Int32(
        bo.buffer_load(
            s_rsrc, ((rg // 2) * k1n + kc // 2) * 64 + ln, vec_width=1, dtype=T.i32
        )
    )
    sc_byte = word.shrui(((kc % 2) * 2 + rg % 2) * 8) & fx.Int32(0xFF)
    return ("mxfp4_atom", (raw, sc_byte), coef, b_word + (lane // 16) * 16)


def mma_units(st, acc, units):
    """acc[4] += coef * (W_chunk @ X_chunk) for every issued unit."""
    xs = st.xs
    v4f = fx.Vector.make_type(4, fx.Float32)
    for unit_format, wv, coef, bw in units:
        if const_expr(callable(coef) and unit_format != "mxfp4_atom"):
            coef = coef()
        if const_expr(unit_format == "mxfp4_atom"):
            # step sp takes K 32 * kl + 8 * sp .. of the lane's block (B words 16 * kl + 4 * sp)
            raw, sc_byte = wv
            c = (
                fx.Vector.from_elements(acc, fx.Float32)
                if coef is None
                else fx.Vector.filled(4, 0.0, fx.Float32)
            )
            sc = (sc_byte << fx.Int32(23)).bitcast(fx.Float32)
            for sp in range_constexpr(4):
                a = mxfp4_to_bf16x8(raw[sp], sc)
                b = fx.ptr_load(xs + (bw + sp * 4), result_type=v4f).bitcast(
                    fx.BFloat16
                )
                c = fx.Vector(rocdl.mfma_f32_16x16x32_bf16(T.vec(4, T.f32), [a, b, c]))
            if const_expr(coef is None):
                acc = [c[e] for e in range(4)]
            else:
                f = coef() if const_expr(callable(coef)) else coef
                acc = [acc[e] + c[e] * f for e in range(4)]
            continue
        c = fx.Vector.filled(4, 0.0, fx.Float32)
        if const_expr(unit_format == "f8f8"):  # one FP8 x FP8 MFMA (E8M0 scales = 1)
            a = fx.Vector.from_elements(
                [wv[h][e] for h in range(2) for e in range(4)], fx.Int32
            )
            bv = [
                fx.Vector(fx.ptr_load(xs + (bw + h * 16), result_type=v4f)).bitcast(
                    fx.Int32
                )
                for h in range(2)
            ]
            b = fx.Vector.from_elements(
                [bv[h][e] for h in range(2) for e in range(4)], fx.Int32
            )
            one = fx.Int32(127)
            c = fx.Vector(
                rocdl.mfma_scale_f32_16x16x128_f8f6f4(
                    T.vec(4, T.f32), [a, b, c, 0, 0, 0, one, 0, one]
                )
            )
        else:  # fp8 / fp8s / bf16 / bf16s: two K32 MFMAs
            for sp in range_constexpr(2):
                if const_expr(unit_format in ("fp8", "fp8s")):
                    a = fp8_to_bf16x8(wv[0][sp * 2], wv[0][sp * 2 + 1])
                else:
                    a = wv[sp].bitcast(fx.BFloat16)
                b = fx.ptr_load(
                    xs + (bw + sp * (4 if unit_format in ("fp8s", "bf16s") else 16)),
                    result_type=v4f,
                ).bitcast(fx.BFloat16)
                c = fx.Vector(rocdl.mfma_f32_16x16x32_bf16(T.vec(4, T.f32), [a, b, c]))
        if const_expr(coef is None):
            acc = [acc[e] + c[e] for e in range(4)]
        else:
            acc = [acc[e] + c[e] * coef for e in range(4)]
    return acc


def run_units(st, make_unit, cpw, batch, pre=None):
    """Software pipelined: issue batch b+1's loads before computing batch b.
    ``pre`` = the already-issued first batch (prefetched before a wait)."""
    mma_units = st.mma_units
    acc = [fx.Float32(0.0) for _ in range(4)]
    starts = list(range(0, cpw, batch))
    cur = pre if pre is not None else [make_unit(c) for c in range(min(batch, cpw))]
    for bi in range_constexpr(len(starts)):
        nxt = None
        if const_expr(bi + 1 < len(starts)):
            n0 = starts[bi + 1]
            nxt = [make_unit(c) for c in range(n0, min(n0 + batch, cpw))]
        acc = mma_units(acc, cur)
        cur = nxt
    return acc


@traced
def reduce_rows(st, R, acc, emit, count=None):
    """Sum per-wave MFMA tiles; emit(row_local, local sample column, value)."""
    S = st.S
    lane = st.lane
    red = st.red
    tid = st.tid
    wave = st.wave
    count = S if count is None else count
    wpr = WAVES // R
    fx.ptr_store(fx.Vector.from_elements(acc, fx.Float32), red + (wave * 64 + lane) * 4)
    gpu.barrier()
    n_out = R * 16 * count
    for i in range_constexpr((n_out + THREADS - 1) // THREADS):
        t = tid + i * THREADS
        if t < n_out:
            rl = t % (R * 16)
            n = t // (R * 16)
            r = rl % 16
            tot = fx.Float32(0.0)
            for j in range_constexpr(wpr):
                ww = (rl // 16) * wpr + j
                tot = tot + lds_ld(red, (ww * 64 + n + 16 * (r // 4)) * 4 + r % 4)
            emit(rl, n, tot)


def emit_out(st, stride):
    outs = st.outs

    def f(rl, n, v):
        lds_st(outs, n * stride + rl, v)

    return f


def n_sel(st, count=None):
    """This lane's local MFMA B column; inactive columns duplicate the last one."""
    S = st.S
    lane = st.lane
    count = S if count is None else count
    return fx.min(lane % 16, count - 1)


# ------------------------------------------------------------------------------ activation staging


def rmsnorm_tail_ks(st, n):
    """This thread's group-of-4 starting indices of n elements. A ragged tail is
    clamped to the last group (idempotent rewrites); ``active`` masks its
    contribution to sums (None when there is no tail)."""
    tid = st.tid
    nq = n // 4
    full = nq // THREADS
    ks = [(tid + i * THREADS) * 4 for i in range(full)]
    active = None
    if const_expr(nq % THREADS):
        w = tid + full * THREADS
        active = w < nq
        ks.append(fx.min(w, nq - 1) * 4)
    return ks, active


@traced
def stage_x_pairs(st, name, n_total, src_of):
    """LDS bf16 X[k] = packed bf16 mailbox ``name`` element src_of(k) for k < n_total
    (src_of contiguous over aligned groups of 4): one 16-byte poll per 4 elements."""
    tid = st.tid
    xs = st.xs
    mb = st.mb
    poll = st.poll
    nq = n_total // 4
    full = nq // THREADS
    vals = poll(
        [(mb(name), src_of((tid + i * THREADS) * 4) // 2, 2) for i in range(full)]
    )
    for i in range_constexpr(full):
        for j in range_constexpr(2):
            lds_st(xs, (tid + i * THREADS) * 2 + j, vals[i][j].bitcast(fx.Float32))
    if const_expr(nq % THREADS):
        w = tid + full * THREADS
        if w < nq:
            v = poll([(mb(name), src_of(w * 4) // 2, 2)])[0]
            for j in range_constexpr(2):
                lds_st(xs, w * 2 + j, v[j].bitcast(fx.Float32))


def quant_scaled(st, a0, a1):
    """Per-wave FP8 quant of a 128-block held as 2 f32 per lane -> (scaled q0, q1, scale)."""
    amax = wave_max(fx.max(fmath.absf(a0), fmath.absf(a1)))
    nz = amax > 0.0
    qs = nz.select(amax * (1.0 / FP8_MAX), fx.Float32(1.0))
    inv = nz.select(rcp(amax) * FP8_MAX, fx.Float32(1.0))
    q0 = fx.min(fx.max(a0 * inv, -FP8_MAX), FP8_MAX)
    q1 = fx.min(fx.max(a1 * inv, -FP8_MAX), FP8_MAX)
    return q0, q1, qs


@traced
def st_f8(st, k, q0, q1, natural=False):
    """LDS FP8 activation bytes k, k + 1 (k even, held by this lane; lane ^ 1 holds
    k ^ 2) in ``f8_word`` order, or in K order when ``natural`` (the order aiter's
    ``shuffle_weight`` (16, 16) matrices take).  Call from the whole wave."""
    lane = st.lane
    xs = st.xs
    w = fx.Int32(rocdl.cvt_pk_fp8_f32(T.i32, q0, q1, fx.Int32(0), False)) & 0xFFFF
    nb = xshfl(w, 1)
    if lane % 2 == 0:
        lds_st(
            xs, k // 4 if natural else f8_word(k), (w | (nb << 16)).bitcast(fx.Float32)
        )


# ---------------------------------------------------------------------------------- TP peer reduce


@traced
def owner_peer_reduce(st, region_base, t, residual, out_fn, tile, *, r_peers=None):
    """``owner_reduce``: the rank owning a tile's rows sums it once and sends the result back."""
    HIDDEN = st.HIDDEN
    S = st.S
    W = st.W
    lane = st.lane
    outs = st.outs
    peer_dst = st.peer_dst
    rank = st.rank
    sym = st.sym
    tag = st.tag
    tid = st.tid
    wave = st.wave
    poll = st.poll
    put_bf = st.put_bf
    pair_count = S * tile // 2
    owner_rank = (t * tile) // (HIDDEN // W)
    owner_words = fx.Vector(
        bo.buffer_load(r_peers, owner_rank * 2, vec_width=2, dtype=T.i32)
    )
    owner_dst = (fx.Int64(uniform(owner_words[1])) << 32) | fx.Int64(
        fx.Uint32(uniform(owner_words[0]))
    )
    if wave == 0:
        for batch in range_constexpr((pair_count + 63) // 64):
            pair = lane + batch * 64
            if pair < pair_count:
                si = pair // (tile // 2)
                ri = (pair % (tile // 2)) * 2
                put_bf(
                    owner_dst + region_base,
                    (rank * S + si) * HIDDEN + t * tile + ri,
                    [lds_ld(outs, si * tile + ri), lds_ld(outs, si * tile + ri + 1)],
                    CM_SYS,
                )
    gpu.barrier()

    if (rank == owner_rank) & (tid < pair_count):
        s = tid // (tile // 2)
        r = (tid % (tile // 2)) * 2
        row = t * tile + r
        r0, r1 = residual(s, row)
        own = sym + region_base
        got = poll(
            [(own, ((src * S + s) * HIDDEN + row) // 2, 1) for src in range(W)],
            "one-as",
        )
        t0 = fx.Float32(0.0)
        t1 = fx.Float32(0.0)
        for src in range_constexpr(W):
            p0, p1 = bf2_f32(got[src][0])
            t0 = t0 + p0
            t1 = t1 + p1
        lds_st(outs, tid, bf16_pair(r0 + t0, r1 + t1))
    gpu.barrier()

    result_tag = tag + fx.Int32(1 << 30)
    if (rank == owner_rank) & (wave < W):
        for batch in range_constexpr((pair_count + 63) // 64):
            pair = lane + batch * 64
            if pair < pair_count:
                s = pair // (tile // 2)
                r = (pair % (tile // 2)) * 2
                packed = lds_ld(outs, pair).bitcast(fx.Int32)
                mailbox = ((owner_rank * S + s) * HIDDEN + t * tile + r) // 2
                bo.buffer_store(
                    fx.Vector.from_elements([packed, result_tag], fx.Int32),
                    rsrc(peer_dst + region_base),
                    mailbox * 2,
                    cache_modifier=CM_SYS,
                )
    gpu.barrier()

    if tid < pair_count:
        s = tid // (tile // 2)
        r = (tid % (tile // 2)) * 2
        row = t * tile + r
        own = sym + region_base
        got = poll(
            [(own, ((owner_rank * S + s) * HIDDEN + row) // 2, 1)],
            "one-as",
            expected_tag=result_tag,
        )
        v0, v1 = bf2_f32(got[0][0])
        out_fn(s, row, v0, v1)


@traced
def peer_reduce(
    st, region, t, residual, out_fn, tile=ROW_TILE, *, owner_reduce=False, r_peers=None
):
    """Push BF16 partials in tagged pairs to every peer, then sum all ranks' pairs from the own
    symmetric buffer in rank order (W = 1: no exchange). ``residual`` is fn(s, row) -> (r0, r1)
    (plain loads, issued first), a mailbox base (pairs s * HIDDEN + row, polled with the peers),
    or None when the caller adds its own. With ``peer_slot`` consecutive epochs use alternating
    ``_part_stride`` slots: a rank cannot finish epoch k + 1 before every peer enters it, so no
    rank reaches k + 2 soon enough to overwrite epoch k while it is still being consumed. With
    ``owner_reduce`` each tile is summed once by the rank that owns its rows and sent back.
    """
    HIDDEN = st.HIDDEN
    S = st.S
    SY = st.SY
    W = st.W
    lane = st.lane
    outs = st.outs
    peer_dst = st.peer_dst
    peer_slot = st.peer_slot
    rank = st.rank
    sym = st.sym
    tid = st.tid
    wave = st.wave
    owner_peer_reduce = st.owner_peer_reduce
    poll = st.poll
    put_bf = st.put_bf
    region_base = fx.Int64(SY[region])
    if const_expr(peer_slot is not None):
        region_base = region_base + fx.Int64(peer_slot) * fx.Int64(SY["_part_stride"])
    if const_expr(owner_reduce and W > 1):
        owner_peer_reduce(region_base, t, residual, out_fn, tile, r_peers=r_peers)
        return
    if const_expr(W > 1):
        # one wave per destination peer
        if wave < W:
            pair_count = S * tile // 2
            for batch in range_constexpr((pair_count + 63) // 64):
                pair = lane + batch * 64
                if pair < pair_count:
                    si = pair // (tile // 2)
                    ri = (pair % (tile // 2)) * 2
                    put_bf(
                        peer_dst + region_base,
                        (rank * S + si) * HIDDEN + t * tile + ri,
                        [
                            lds_ld(outs, si * tile + ri),
                            lds_ld(outs, si * tile + ri + 1),
                        ],
                        CM_SYS,
                    )
        gpu.barrier()
    if tid < S * tile // 2:
        s = tid // (tile // 2)
        r = (tid % (tile // 2)) * 2
        row = t * tile + r
        r0 = fx.Float32(0.0)
        r1 = fx.Float32(0.0)
        if const_expr(callable(residual)):
            r0, r1 = residual(s, row)
        v0 = lds_ld(outs, s * tile + r)
        v1 = lds_ld(outs, s * tile + r + 1)
        if const_expr(W == 1):
            parts = [(v0, v1)]
            got = []
            if const_expr(residual is not None and not callable(residual)):
                got = poll([(residual, (s * HIDDEN + row) // 2, 1)])
        else:
            own = sym + region_base
            specs = [(own, ((src * S + s) * HIDDEN + row) // 2, 1) for src in range(W)]
            if const_expr(residual is not None and not callable(residual)):
                specs.append((residual, (s * HIDDEN + row) // 2, 1))
            got = poll(specs, "one-as")
            parts = [bf2_f32(v[0]) for v in got[:W]]
            got = got[W:]
        if const_expr(residual is not None and not callable(residual)):
            r0, r1 = bf2_f32(got[0][0])
        t0 = fx.Float32(0.0)
        t1 = fx.Float32(0.0)
        for src in range_constexpr(W):
            t0 = t0 + parts[src][0]
            t1 = t1 + parts[src][1]
        out_fn(s, row, r0 + t0, r1 + t1)


HELPERS = (
    mb,
    put,
    put2,
    put_bf,
    poll,
    get,
    getf,
    getf_many,
    get2_many,
    start,
    block_sums,
    block_sum,
    unit_fp8,
    unit_f8f8,
    unit_bf16,
    unit_bf16_rows,
    unit_mxfp4_atom,
    mma_units,
    run_units,
    reduce_rows,
    emit_out,
    n_sel,
    rmsnorm_tail_ks,
    stage_x_pairs,
    quant_scaled,
    st_f8,
    owner_peer_reduce,
    peer_reduce,
)


class LaunchState:
    """One launch's values for the shared helpers, and the helpers bound to it (``st.put`` ...).

    ``source_key`` is the kernel's captured ``SHARED_SOURCE_KEY`` (see there)."""

    def __init__(
        self,
        *,
        source_key,
        S,
        tid,
        lane,
        wave,
        bid,
        G,
        base,
        scratch,
        SC,
        tag,
        red,
        outs,
        xs,
        bias=None,
        N_EXPERTS=None,
        rank=None,
        W=1,
        HIDDEN=None,
        peer_dst=None,
        sym=None,
        SY=None,
        peer_slot=None,
        eps=EPS,
    ):
        assert (
            source_key == SHARED_SOURCE_KEY
        ), "the kernel was built against older shared helper sources"
        self.S = S
        self.tid = tid
        self.lane = lane
        self.wave = wave
        self.bid = bid
        self.G = G
        self.base = base
        self.scratch = scratch
        self.SC = SC
        self.tag = tag
        self.red = red
        self.outs = outs
        self.xs = xs
        self.bias = bias
        self.N_EXPERTS = N_EXPERTS
        self.rank = rank
        self.W = W
        self.HIDDEN = HIDDEN
        self.peer_dst = peer_dst
        self.sym = sym
        self.SY = SY
        self.peer_slot = peer_slot
        self.eps = eps
        for fn in HELPERS:
            setattr(self, fn.__name__, partial(fn, self))
