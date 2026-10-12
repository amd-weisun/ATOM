# SPDX-License-Identifier: MIT
"""Bench-only capture of Qwen3.5 decoder layers for the MonoKernel equivalence check (qwen_mono_bench).

Set QWEN_CAPTURE_LAYERS="0,3" and QWEN_CAPTURE_DIR=<dir> (eager mode: graph replay runs no Python).
Each decode forward of a listed layer, up to QWEN_CAPTURE_STEPS per layer, saves on every TP rank:
the input stream (hidden + residual), positions, the output stream, and the layer state before / after
it: a GDN layer's conv / ssm slots (read and written), a full-attention layer's KV blocks of the batch
(block tables, context lengths, both caches and their scales, as a compact copy).
"""
import os

import torch

_LAYERS = {int(x) for x in os.environ.get("QWEN_CAPTURE_LAYERS", "").split(",") if x}
_DIR = os.environ.get("QWEN_CAPTURE_DIR", "")
_STEPS = int(os.environ.get("QWEN_CAPTURE_STEPS", "3"))
_count: dict = {}


def enabled(layer) -> bool:
    return layer.layer_idx in _LAYERS and _count.get(layer.layer_idx, 0) < _STEPS


def _cache(layer):
    from atom.utils.forward_context import get_forward_context

    ctx = get_forward_context()
    mod = layer.linear_attn.attn if layer.layer_type == "linear_attention" else layer.self_attn.attn
    return ctx, ctx.kv_cache_data[f"layer_{mod.layer_num}"]


def _state(layer, which):
    ctx, lc = _cache(layer)
    md = ctx.attn_metadata
    if layer.layer_type == "linear_attention":
        g = md.gdn_metadata
        slots = (g.non_spec_state_indices_in_tensor if which == "in" else g.non_spec_state_indices_tensor).long()
        return dict(slots=slots.clone(), conv=lc.k_cache[slots].clone(), ssm=lc.v_cache[slots].clone())
    bt = md.block_tables.clone()
    ctx_lens = md.context_lens.clone()
    blocks = torch.unique(torch.cat([bt[i, : (int(ctx_lens[i]) + 15) // 16] for i in range(bt.shape[0])]))
    return dict(
        block_tables=bt,
        context_lens=ctx_lens,
        blocks=blocks,
        k_cache=lc.k_cache[blocks].clone(),
        v_cache=lc.v_cache[blocks].clone(),
        k_scale=lc.k_scale[blocks].clone(),
        v_scale=lc.v_scale[blocks].clone(),
    )


def run(layer, fwd, positions, hidden_states, residual):
    """``fwd`` = the original forward. Captures only pure decode steps."""
    from atom.utils.forward_context import get_forward_context

    if get_forward_context().context.is_prefill:
        return fwd(positions, hidden_states, residual)
    x_in = hidden_states.clone() if residual is None else (hidden_states.float() + residual.float()).bfloat16()
    pre = _state(layer, "in")
    pos = positions.clone()
    h_out, r_out = fwd(positions, hidden_states, residual)
    torch.cuda.synchronize()
    post = _state(layer, "out")
    from aiter.dist.parallel_state import get_tp_group

    rank = get_tp_group().rank_in_group
    k = _count.get(layer.layer_idx, 0)
    _count[layer.layer_idx] = k + 1
    os.makedirs(_DIR, exist_ok=True)
    torch.save(
        dict(
            layer=layer.layer_idx,
            layer_type=layer.layer_type,
            rank=rank,
            positions=pos.cpu(),
            x_in=x_in.cpu(),
            x_out=(h_out.float() + r_out.float()).bfloat16().cpu(),
            pre={k_: v.cpu() for k_, v in pre.items()},
            post={k_: v.cpu() for k_, v in post.items()},
        ),
        os.path.join(_DIR, f"r{rank}_L{layer.layer_idx}_s{k}.pt"),
    )
    return h_out, r_out


# ---- layer timing (QWEN_TIME_LAYERS=1): baseline ATOM's real layers, graph-replayed, at each new decode batch size
_TIME = os.environ.get("QWEN_TIME_LAYERS") == "1"
_GDN_CHAIN = [0, 1, 2, 4, 5, 6]  # six distinct layers of each kind: weights not cache-hot
_FULL_CHAIN = [3, 7, 11, 15, 19, 23]
_LAST = int(os.environ.get("QWEN_LAST_LAYER", "59"))  # the model's last decoder layer
_layers: dict = {}
_timed: set = set()
_busy = False


def timing(layer) -> bool:
    return _TIME and not _busy


def time_run(layer, fwd, positions, hidden_states, residual):
    """Register every layer; at the last one of a decode step with a new batch size, time the chains."""
    global _busy
    import time

    from aiter.dist.parallel_state import get_tp_group, graph_capture

    from atom.utils.forward_context import get_forward_context

    _layers[layer.layer_idx] = layer
    out = fwd(positions, hidden_states, residual)
    S = hidden_states.shape[0]
    if get_forward_context().context.is_prefill or layer.layer_idx != _LAST or S in _timed:
        return out
    _timed.add(S)
    _busy = True
    try:
        res = {}
        for name, chain in (("GDN", _GDN_CHAIN), ("full", _FULL_CHAIN)):
            h = hidden_states.clone()
            r = (residual if residual is not None else hidden_states).clone()

            def step():
                for idx in chain:
                    _layers[idx].forward(positions, h, r)

            step()
            torch.cuda.synchronize()
            reps = 4
            with graph_capture() as ctx:
                g = torch.cuda.CUDAGraph()
                with torch.cuda.graph(g, stream=ctx.stream):
                    for _ in range(reps):
                        step()
            torch.cuda.synchronize()
            for _ in range(5):
                g.replay()
            torch.cuda.synchronize()
            n = 20
            t0 = time.perf_counter()
            for _ in range(n):
                g.replay()
            torch.cuda.synchronize()
            res[name] = (time.perf_counter() - t0) / n / reps / len(chain) * 1e6
            del g
        if get_tp_group().rank_in_group == 0:
            ctx_len = get_forward_context().attn_metadata.context_lens
            print(f"ATOMLAYER S={S} ctx {ctx_len.tolist() if ctx_len is not None else '?'}: "
                  f"GDN {res['GDN']:.1f} us/layer  full {res['full']:.1f} us/layer", flush=True)
    finally:
        _busy = False
    return out
