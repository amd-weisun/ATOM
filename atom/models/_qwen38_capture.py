# SPDX-License-Identifier: MIT
"""Bench-only capture of Qwen3.8 decoder layers for the MonoKernel equivalence check (qwen_mono_bench).

Set QWEN_CAPTURE_LAYERS="0,3" and QWEN_CAPTURE_DIR=<dir> (eager mode: graph replay runs no Python). Each decode
forward of a listed layer, up to QWEN_CAPTURE_STEPS per layer, saves the bundle in and out with the deferred
combines materialized (in = combine(hidden, pending), out = combine(h, pending_out)), positions, and the layer
state before / after: a GDN layer's conv / ssm slots, a QSA layer's touched blocks of its four caches.
"""
import os

import torch

_LAYERS = {int(x) for x in os.environ.get("QWEN_CAPTURE_LAYERS", "").split(",") if x}
_DIR = os.environ.get("QWEN_CAPTURE_DIR", "")
_STEPS = int(os.environ.get("QWEN_CAPTURE_STEPS", "3"))
_count: dict = {}


def enabled(layer) -> bool:
    return layer.layer_idx in _LAYERS and _count.get(layer.layer_idx, 0) < _STEPS


def _state(layer):
    from atom.utils.forward_context import get_forward_context

    ctx = get_forward_context()
    md = ctx.attn_metadata
    if layer.layer_type == "linear_attention":
        lc = ctx.kv_cache_data[f"layer_{layer.linear_attn.attn.layer_num}"]
        g = md.gdn_metadata
        si = g.non_spec_state_indices_in_tensor.long()
        so = g.non_spec_state_indices_tensor.long()
        return dict(slots_in=si.clone(), slots_out=so.clone(), conv_in=lc.k_cache[si].clone(),
                    ssm_in=lc.v_cache[si].clone(), conv_out=lc.k_cache[so].clone(), ssm_out=lc.v_cache[so].clone())
    a = layer.self_attn
    bt = md.block_tables.clone()
    ctx_lens = md.context_lens.clone()
    blocks = torch.unique(torch.cat([bt[i, : (int(ctx_lens[i]) + 15) // 16] for i in range(bt.shape[0])]))
    return dict(block_tables=bt, context_lens=ctx_lens, blocks=blocks, k_cache=a.k_cache[blocks].clone(),
                v_cache=a.v_cache[blocks].clone(), raw=a.raw_key_cache[blocks].clone(),
                compressed=a.compressed_key_cache[blocks].clone())


def run(layer, fwd, positions, hidden_states, input_ids, pending):
    """``fwd`` = the original forward. Captures only pure decode steps."""
    from atom.utils.forward_context import get_forward_context

    if get_forward_context().context.is_prefill:
        return fwd(positions, hidden_states, input_ids, pending)
    hc = layer.attn_hyper_connection
    x_in = hidden_states if pending is None else hc.apply_pending(hidden_states, pending)
    x_in = x_in.clone()
    pre = _state(layer)
    pos = (positions[0] if positions.dim() == 2 else positions).clone()
    h_out, pend_out = fwd(positions, hidden_states, input_ids, pending)
    torch.cuda.synchronize()
    x_out = h_out if pend_out is None else hc.apply_pending(h_out, pend_out)
    post = _state(layer)
    k = _count.get(layer.layer_idx, 0)
    _count[layer.layer_idx] = k + 1
    os.makedirs(_DIR, exist_ok=True)
    torch.save(
        dict(layer=layer.layer_idx, layer_type=layer.layer_type, positions=pos.cpu(), x_in=x_in.cpu(),
             x_out=x_out.clone().cpu(), pre={k_: v.cpu() for k_, v in pre.items()},
             post={k_: v.cpu() for k_, v in post.items()}),
        os.path.join(_DIR, f"L{layer.layer_idx}_s{k}.pt"),
    )
    return h_out, pend_out
