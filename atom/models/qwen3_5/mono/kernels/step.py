# SPDX-License-Identifier: MIT
# Copyright (C) 2026, Advanced Micro Devices, Inc. All rights reserved.
"""The step advance: ``step += 1`` plus a scrub of stale mailbox pairs."""

import flydsl.compiler as flyc
import flydsl.expr as fx
from aiter.ops.flydsl.kernels import buffer_ops as bo
from flydsl.expr import gpu, range_constexpr
from flydsl.expr.typing import Int64, T

from atom.models.qwen3_5.mono.kernels.ops import THREADS, rsrc, uniform

# tags are step * LAYER_SLOTS + layer + 1: a step's launches take distinct layer values below it
MAX_LAYERS_PER_STEP = 256
LAYER_SLOTS = MAX_LAYERS_PER_STEP
SCRUB_PAIRS = (
    THREADS  # mailbox pairs each step advance checks: one per thread, one round trip
)

_CURRENT_STREAM = fx.Stream(None)


def scrub_period(n_pairs: int) -> int:
    """Steps for the step advance to visit every one of ``n_pairs`` pairs: a power
    of two, so ``step % period`` stays continuous through the int32 wrap."""
    return 1 << max(0, -(-n_pairs // SCRUB_PAIRS) - 1).bit_length()


def build_advance_step(scr_pairs: int, sym_pairs: int):
    """The ``@flyc.jit`` step advance for one scratch: ``step += 1`` plus a scrub.

    Int32 tags wrap, and a mailbox left unwritten that long would read as fresh, so each
    advance zeroes (CAS) pairs older than the last step in one of ``scrub_period``
    slices; tags are never 0, and a peer one launch ahead only writes newer tags."""
    n = scr_pairs + sym_pairs
    period = scrub_period(n)
    chunk = -(-n // period)
    per_thread = -(-chunk // THREADS)

    @flyc.kernel(known_block_size=[THREADS, 1, 1])
    def advance_kernel(step: Int64, scratch: Int64, sym: Int64):
        tid = fx.thread_idx.x

        def scrub(addr, new_base):
            """Zero the pair at ``addr`` if its tag is nonzero and at most ``new_base``."""
            ptr = fx.inttoptr(
                fx.PointerType.get(fx.Int64.ir_type, fx.AddressSpace.Global, 8), addr
            )
            old = fx.Int64(
                fx.generic_load(
                    ptr, memory_order=fx.AtomicOrdering.Monotonic, syncscope="agent"
                )
            )
            tg = fx.Int32(old >> 32)  # a pair is (value, tag): the tag is the high word
            if (tg != 0) & ((new_base - tg) >= 0):
                fx.atomic_cas(ptr, old, fx.Int64(0))

        s = uniform(bo.buffer_load(rsrc(step), 0, vec_width=1, dtype=T.i32))
        new_base = s * LAYER_SLOTS  # every tag of steps < s is at most this
        base = (s & (period - 1)) * chunk
        for j in range_constexpr(per_thread):
            o = j * THREADS + tid
            i = base + o
            if (o < chunk) & (i < n):
                if i < scr_pairs:
                    scrub(scratch + fx.Int64(i) * 8, new_base)
                else:
                    scrub(sym + fx.Int64(i - scr_pairs) * 8, new_base)
        gpu.barrier()
        if tid == 0:
            bo.buffer_store(s + 1, rsrc(step), 0)

    @flyc.jit
    def advance_step(
        step: Int64, scratch: Int64, sym: Int64, stream: fx.Stream = _CURRENT_STREAM
    ):
        advance_kernel(step, scratch, sym).launch(
            grid=(1,), block=(THREADS,), stream=stream
        )

    return advance_step
