# SPDX-License-Identifier: MIT
"""DeepSeek-V3/R1 decode through the FlyDSL MLA+MoE mega kernel (opt-in).

Enabled with ``ATOM_DSV3_MEGAKERNEL=1`` for ``DeepseekV3ForCausalLM`` under TP, with FP8
block-quantized checkpoints.  Layers ``first_k_dense_replace .. L-1`` (attention + MoE, the
whole decoder layer including both TP all-reduces) are each one kernel launch; the leading
dense layers, the embedding and the final norm keep ATOM's own modules.  Prefill and any
decode batch the kernel does not cover (S not in 1/2/4/8, S > 8, ...) run ATOM's normal
compiled path unchanged.

The kernel keeps its own packed copy of the layer weights (MFMA tile order), built here
straight from the checkpoint shards: ATOM's loaded parameters are post-processed
(shuffled/fused) for its own kernels and are not reusable.

Kernel-side contract (``kernels/mla_moe_layer``, ``paged=True``): S independent sequences,
one new token each, over ATOM's paged MLA pool (rows of 576 bf16: 512 latent | 64 k_pe,
i.e. ``--kv_cache_dtype bf16``), addressed with ATOM's own ``kv_indptr``/``kv_indices``/
``slot_mapping``.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import os
import time

import torch

from atom.utils import envs
from atom.utils.forward_context import get_forward_context

try:  # the kernels live in the FlyDSL checkout (``kernels`` package)
    from kernels.mla_moe_layer.config import KV_LORA, N_EXPERTS, PE_DIM
    from kernels.mla_moe_layer.layer import SharedReuseMlaMoeLayer
    from kernels.mla_moe_layer.reference import LayerWeights
except ImportError:  # pragma: no cover - reported when the feature is enabled
    SharedReuseMlaMoeLayer = None

from aiter.dist.communication_op import tensor_model_parallel_all_reduce
from aiter.dist.parallel_state import get_tp_group

logger = __import__("logging").getLogger("atom")

SUPPORTED_S = (1, 2, 4, 8)
FP8 = torch.float8_e4m3fn


class _Checkpoint:
    """Lazy per-shard safetensors access with row/column slicing (mmap, no full reads)."""

    def __init__(self, path: str):
        from safetensors import safe_open

        self._safe_open = safe_open
        self.path = path
        with open(os.path.join(path, "model.safetensors.index.json")) as f:
            self.weight_map = json.load(f)["weight_map"]
        self._files = {}

    def get(self, name: str, rows: tuple[int, int] | None = None, cols: tuple[int, int] | None = None):
        shard = self.weight_map[name]
        if shard not in self._files:
            self._files[shard] = self._safe_open(os.path.join(self.path, shard), framework="pt", device="cpu")
        sl = self._files[shard].get_slice(name)
        if rows is None and cols is None:
            return sl[:] if len(sl.get_shape()) == 1 else sl[:, :]
        if len(sl.get_shape()) == 1:
            return sl[rows[0] : rows[1]]
        r = slice(*rows) if rows else slice(None)
        c = slice(*cols) if cols else slice(None)
        return sl[r, c]


def _dev(t: torch.Tensor, dev) -> torch.Tensor:
    return t.to(dev, non_blocking=False)


def load_layer_weights(ck: _Checkpoint, cfg, layer: int, tp_rank: int, tp: int, dev) -> "LayerWeights":
    """One rank's TP shard of one MoE layer, in the kernel's row-major FP8 + block-scale layout."""
    H = cfg.num_attention_heads // tp  # local heads
    hidden, q_lora = cfg.hidden_size, cfg.q_lora_rank
    nope, pe, v_dim = cfg.qk_nope_head_dim, cfg.qk_rope_head_dim, cfg.v_head_dim
    inter = cfg.moe_intermediate_size // tp
    pre = f"model.layers.{layer}."
    sa = pre + "self_attn."
    t: dict[str, torch.Tensor] = {}
    bf = torch.bfloat16

    t["g_in"] = _dev(ck.get(pre + "input_layernorm.weight"), dev).to(bf)
    t["g_post"] = _dev(ck.get(pre + "post_attention_layernorm.weight"), dev).to(bf)
    t["g_q"] = _dev(ck.get(sa + "q_a_layernorm.weight"), dev).to(bf)
    t["g_kv"] = _dev(ck.get(sa + "kv_a_layernorm.weight"), dev).to(bf)

    # q_a and kv_a are replicated; the kernel wants them fused (q_lora rows then kv_lora+pe rows)
    t["w_qkv_a"] = _dev(torch.cat([ck.get(sa + "q_a_proj.weight"), ck.get(sa + "kv_a_proj_with_mqa.weight")]), dev)
    t["s_qkv_a"] = _dev(
        torch.cat([ck.get(sa + "q_a_proj.weight_scale_inv"), ck.get(sa + "kv_a_proj_with_mqa.weight_scale_inv")]), dev
    ).float()

    qk = nope + pe
    t["w_q_b"] = _dev(ck.get(sa + "q_b_proj.weight", rows=(tp_rank * H * qk, (tp_rank + 1) * H * qk)), dev)
    t["s_q_b"] = _dev(
        ck.get(sa + "q_b_proj.weight_scale_inv", rows=(tp_rank * H * qk // 128, (tp_rank + 1) * H * qk // 128)), dev
    ).float()

    # kv_b: per head [nope rows of W_UK^T-to-be | v rows of W_UV], each 128 rows = one scale block
    kvb_rows = (tp_rank * H * (nope + v_dim), (tp_rank + 1) * H * (nope + v_dim))
    kvb = _dev(ck.get(sa + "kv_b_proj.weight", rows=kvb_rows), dev).view(H, nope + v_dim, KV_LORA)
    kvb_s = _dev(
        ck.get(sa + "kv_b_proj.weight_scale_inv", rows=(kvb_rows[0] // 128, kvb_rows[1] // 128)), dev
    ).float()
    assert nope == 128 and v_dim == 128, "kv_b scale blocks assume 128-row nope/v halves"
    kvb_s = kvb_s.view(H, 2, KV_LORA // 128)
    # absorbed W_UK is stored transposed: uk[h] = W_nope[h]^T  [KV_LORA, nope]; scale rows follow the
    # lora blocks and the two 64-wide nope column blocks share the 128-block scale
    t["w_uk"] = kvb[:, :nope, :].transpose(1, 2).contiguous().view(H * KV_LORA, nope)
    t["s_uk"] = kvb_s[:, 0, :].reshape(H * KV_LORA // 128, 1).expand(-1, nope // 64).contiguous()
    t["w_uv"] = kvb[:, nope:, :].contiguous().view(H * v_dim, KV_LORA)
    t["s_uv"] = kvb_s[:, 1, :].contiguous().view(H * v_dim // 128, KV_LORA // 128)

    o_cols = (tp_rank * H * v_dim, (tp_rank + 1) * H * v_dim)
    t["w_o"] = _dev(ck.get(sa + "o_proj.weight", cols=o_cols), dev)
    t["s_o"] = _dev(ck.get(sa + "o_proj.weight_scale_inv", cols=(o_cols[0] // 128, o_cols[1] // 128)), dev).float()

    t["w_r"] = _dev(ck.get(pre + "mlp.gate.weight"), dev).to(bf)
    t["bias"] = _dev(ck.get(pre + "mlp.gate.e_score_correction_bias"), dev).float()

    # experts 0..255 then the shared expert (index 256); expert rows/cols are sharded on the intermediate dim
    r0, r1 = tp_rank * inter, (tp_rank + 1) * inter
    ug_q = torch.empty(N_EXPERTS + 1, 2 * inter, hidden, dtype=FP8, device=dev)
    ug_s = torch.empty(N_EXPERTS + 1, 2 * inter // 128, hidden // 128, dtype=torch.float32, device=dev)
    dn_q = torch.empty(N_EXPERTS + 1, hidden, inter, dtype=FP8, device=dev)
    dn_s = torch.empty(N_EXPERTS + 1, hidden // 128, inter // 128, dtype=torch.float32, device=dev)
    for e in range(N_EXPERTS + 1):
        base = pre + (f"mlp.experts.{e}." if e < N_EXPERTS else "mlp.shared_experts.")
        for half, proj in enumerate(("gate_proj", "up_proj")):
            ug_q[e, half * inter : (half + 1) * inter] = _dev(ck.get(base + f"{proj}.weight", rows=(r0, r1)), dev)
            ug_s[e, half * inter // 128 : (half + 1) * inter // 128] = _dev(
                ck.get(base + f"{proj}.weight_scale_inv", rows=(r0 // 128, r1 // 128)), dev
            )
        dn_q[e] = _dev(ck.get(base + "down_proj.weight", cols=(r0, r1)), dev)
        dn_s[e] = _dev(ck.get(base + "down_proj.weight_scale_inv", cols=(r0 // 128, r1 // 128)), dev)
    t["w_ug"], t["s_ug"], t["w_dn"], t["s_dn"] = ug_q, ug_s, dn_q, dn_s
    return LayerWeights(H, t, hidden=hidden, q_lora=q_lora, nope_dim=nope, v_dim=v_dim)


def _cache_dir(config, tp: int, rank: int) -> str | None:
    """Node-local cache of this rank's PACKED layer weights (``ATOM_DSV3_MEGAKERNEL_CACHE``, "" disables).

    Repeat launches then read ~1.4 GB/layer of packed tensors from local disk / page cache instead of doing
    strided checkpoint reads + packing (which alone took 3-13 min/launch, worst on rank 0).  The key hashes
    everything the packed bytes depend on, so a packing or loader change starts a fresh cache."""
    root = envs.ATOM_DSV3_MEGAKERNEL_CACHE
    if not root:
        return None
    import kernels.mla_moe_layer.packing as packing

    h = hashlib.sha256()
    h.update(inspect.getsource(packing).encode())
    h.update(inspect.getsource(load_layer_weights).encode())
    h.update(f"{config.model}|tp{tp}".encode())
    d = os.path.join(root, f"{os.path.basename(config.model.rstrip('/'))}_tp{tp}_{h.hexdigest()[:12]}", f"rank{rank}")
    os.makedirs(d, exist_ok=True)
    return d


def _save_layer(path: str, W, packed: dict) -> None:
    from safetensors.torch import save_file

    tensors = {f"t.{k}": v.contiguous().cpu() for k, v in W.t.items()}
    tensors.update({f"p.{k}": v.contiguous().cpu() for k, v in packed.items()})
    tmp = f"{path}.tmp{os.getpid()}"
    save_file(tensors, tmp, metadata={"heads": str(W.heads)})
    os.replace(tmp, path)


def _load_cached_layer(path: str, cfg, dev):
    from safetensors import safe_open
    from safetensors.torch import load_file

    with safe_open(path, framework="pt") as f:
        heads = int(f.metadata()["heads"])
    raw = load_file(path, device=str(dev))
    t = {k[2:]: v for k, v in raw.items() if k.startswith("t.")}
    packed = {k[2:]: v for k, v in raw.items() if k.startswith("p.")}
    W = LayerWeights(
        heads, t, hidden=cfg.hidden_size, q_lora=cfg.q_lora_rank, nope_dim=cfg.qk_nope_head_dim, v_dim=cfg.v_head_dim
    )
    return W, packed


_DRAFT_LAYERS: dict[str, tuple] = {}


def _draft_layer_op(hidden_states: torch.Tensor, positions: torch.Tensor, layer_name: str) -> torch.Tensor:
    """Draft-model decoder layer + shared_head.norm.  An opaque custom op so the compiled MTP model keeps a
    single graph (a graph break would trip the one-graph compile backend).  Steps the kernel does not cover
    (the draft's prefill pass, unsupported shapes) run ATOM's own ``mtp_block`` eagerly here."""
    mega, layer = _DRAFT_LAYERS[layer_name]
    out = mega.draft_layer(layer, hidden_states, positions)
    if out is None:
        hs, residual = layer.mtp_block(positions=positions, hidden_states=hidden_states, residual=None)
        out, _ = layer.shared_head.norm(hs, residual)
    return out


def _draft_layer_op_fake(hidden_states: torch.Tensor, positions: torch.Tensor, layer_name: str) -> torch.Tensor:
    return torch.empty_like(hidden_states)


from atom.utils.custom_register import direct_register_custom_op  # noqa: E402

direct_register_custom_op(
    op_name="dsv3_mega_draft_layer",
    op_func=_draft_layer_op,
    mutates_args=(),
    fake_impl=_draft_layer_op_fake,
    tags=(torch.Tag.needs_fixed_stride_order,),
)


class DSV3MegaKernel:
    """Per-rank owner of the kernel layers; ``forward`` replaces the model body for covered decode steps."""

    def __init__(self, lm, config):
        if SharedReuseMlaMoeLayer is None:
            raise ImportError("ATOM_DSV3_MEGAKERNEL=1 needs the FlyDSL checkout on PYTHONPATH (package `kernels`)")
        hf = config.hf_config
        self.hf = hf
        self.model = lm.model
        self.first = hf.first_k_dense_replace
        self.n_layers = hf.num_hidden_layers
        tp = get_tp_group()
        self.tp, self.rank = tp.world_size, tp.rank_in_group
        self.dev = torch.device("cuda", torch.cuda.current_device())
        assert hf.n_routed_experts == N_EXPERTS and hf.n_shared_experts == 1, "kernel is fixed to 256+1 experts"
        assert hf.scoring_func == "sigmoid" and hf.norm_topk_prob and hf.routed_scaling_factor == 2.5
        assert config.kv_cache_dtype in ("bf16", "fp8"), "the kernel reads/writes a bf16 or fp8 (unit-scale e4m3) MLA cache"
        self.kv_fp8 = config.kv_cache_dtype == "fp8"
        spl = envs.ATOM_DSV3_MEGAKERNEL_SPLITS
        # 64 splits win at batch<=4 (up to ~1.8x at 128K ctx); at batch 8 the 512 split tasks overflow the 256 CTAs
        self.splits = {S: (64 if S <= 4 else 32) if spl == "auto" else int(spl) for S in SUPPORTED_S}
        self.check = envs.ATOM_DSV3_MEGAKERNEL_CHECK
        attn = self.model.layers[self.first].self_attn
        self.softmax_scale = float(attn.scaling)
        self.eps = float(hf.rms_norm_eps)
        # ATOM defers each layer's TP all-reduce into the next layer's fused AR+RMSNorm, so the
        # dense layers hand over an UNREDUCED partial; the kernel returns a fully reduced output
        self.defer_ar = bool(self.model.layers[self.first].input_layernorm.fused_allreduce) and self.tp > 1
        cos = attn.rotary_emb.cos_cache
        sin = attn.rotary_emb.sin_cache
        self.cos = cos.reshape(cos.shape[0], -1).float().contiguous().to(self.dev)
        self.sin = sin.reshape(sin.shape[0], -1).float().contiguous().to(self.dev)
        assert self.cos.shape[1] == PE_DIM // 2
        self.group = tp.cpu_group
        # speculative decoding verifies Q = k+1 tokens per sequence per step; plain decode is Q = 1
        spec = getattr(config, "speculative_config", None)
        self.qs = (1,) + ((spec.num_speculative_tokens + 1,) if spec is not None and spec.num_speculative_tokens else ())
        # (S total tokens, Q tokens per sequence) -> one kernel layer per MoE layer
        self.variants = [(S, Q) for Q in self.qs for S in SUPPORTED_S if S % Q == 0]
        self.by_sq: dict[tuple[int, int], list] = {v: [] for v in self.variants}
        # MTP draft layer (the checkpoint's layer ``num_hidden_layers``): same shape as a target MoE layer
        self.has_draft = spec is not None and bool(spec.num_speculative_tokens)
        self.draft_by_sq: dict[tuple[int, int], SharedReuseMlaMoeLayer] = {}
        self.draft_stats: dict = {}
        self._load_layers(config)
        self._kv_pools: dict[int, torch.Tensor] = {}
        self._zero_res: dict[int, torch.Tensor] = {}

    # ------------------------------------------------------------------ setup
    def _load_layers(self, config):
        ck = _Checkpoint(config.model)
        t0 = time.perf_counter()
        # 8 ranks x default (~all cores) intra-op threads oversubscribe the node: 4x slower loads
        prev_threads = torch.get_num_threads()
        torch.set_num_threads(4)
        first_by_v: dict[tuple[int, int], SharedReuseMlaMoeLayer] = {}
        cdir = _cache_dir(config, self.tp, self.rank)
        n_hit = 0
        layer_ids = list(range(self.first, self.n_layers)) + ([self.n_layers] if self.has_draft else [])
        for li in layer_ids:
            cpath = os.path.join(cdir, f"layer{li}.safetensors") if cdir else None
            packed0 = None
            if cpath is not None and os.path.exists(cpath):
                W, packed0 = _load_cached_layer(cpath, self.hf, self.dev)
                n_hit += 1
            else:
                W = load_layer_weights(ck, self.hf, li, self.rank, self.tp, self.dev)
            base = None
            for S, Q in self.variants:
                op = SharedReuseMlaMoeLayer(
                    W,
                    S,
                    rank=self.rank,
                    npes=self.tp,
                    group=self.group,
                    topk=self.splits[S] * 64,
                    moe_mode="w8a8",
                    n_groups=self.hf.n_group,
                    topk_groups=self.hf.topk_group,
                    paged=True,
                    kv_fp8=self.kv_fp8,
                    q_per_seq=Q,
                    eps=self.eps,
                    softmax_scale=self.softmax_scale,
                    free_unpacked=(base is None and packed0 is None),
                    reuse=first_by_v.get((S, Q)),
                    packed=packed0 if base is None else base.packed,
                )
                if base is None:
                    base = op
                first_by_v.setdefault((S, Q), op)
                if li == self.n_layers:
                    self.draft_by_sq[(S, Q)] = op
                else:
                    self.by_sq[(S, Q)].append(op)
            if cpath is not None and packed0 is None:
                _save_layer(cpath, W, base.packed)
            torch.cuda.synchronize()
            if self.rank == 0 and (li - self.first) % 8 == 0 and li < self.n_layers:
                logger.info(
                    "[dsv3-mega] layer %d/%d loaded (%.0fs)", li, self.n_layers - 1, time.perf_counter() - t0
                )
        torch.set_num_threads(prev_threads)
        logger.info("[dsv3-mega] rank %d: %d layers ready in %.0fs (%d from the packed-weight cache)", self.rank, len(layer_ids), time.perf_counter() - t0, n_hit)

    # --------------------------------------------------------------- dispatch
    def applies(self, positions: torch.Tensor, intermediate_tensors, inputs_embeds) -> bool:
        if intermediate_tensors is not None or inputs_embeds is not None:
            return False
        ctx = get_forward_context()
        c = ctx.context
        if c is None or c.is_prefill or getattr(c, "is_draft", False):
            return False
        md = ctx.attn_metadata
        if md is None or md.kv_indptr is None or md.kv_indices is None or md.slot_mapping is None:
            return False
        if not self._kv_ready():
            return False  # engine warmup runs before the KV cache is allocated
        return (positions.shape[0], int(md.max_seqlen_q) or 1) in self.by_sq

    def _kv_ready(self) -> bool:
        if not getattr(self, "_kv_bound", False):
            self._kv_bound = any(
                isinstance(kc := getattr(mod, "kv_cache", None), torch.Tensor) and kc.dim() == 3
                for mod in self.model.layers[self.first].self_attn.modules()
            )
        return self._kv_bound

    def _pool(self, idx: int) -> torch.Tensor:
        pool = self._kv_pools.get(idx)
        if pool is None:
            kv = next(
                kc
                for mod in self.model.layers[idx].self_attn.modules()
                if isinstance(kc := getattr(mod, "kv_cache", None), torch.Tensor) and kc.dim() == 3
            )
            want = torch.float8_e4m3fn if self.kv_fp8 else torch.bfloat16
            assert kv.dtype == want and kv.shape[-1] == KV_LORA + PE_DIM, (kv.dtype, kv.shape)
            pool = self._kv_pools[idx] = kv.view(-1, KV_LORA + PE_DIM)
        return pool

    # ---------------------------------------------------------------- forward
    def forward(self, input_ids: torch.Tensor, positions: torch.Tensor) -> torch.Tensor:
        m = self.model
        S = positions.shape[0]
        Q = int(get_forward_context().attn_metadata.max_seqlen_q) or 1
        hs = m.get_input_embeddings(input_ids)
        residual = None
        for i in range(self.first):
            hs, residual = m.layers[i](positions, hs, residual)
        h = (tensor_model_parallel_all_reduce(hs) if self.defer_ar else hs) + residual
        # Padded rows of a graph batch (real bs 3/5/6/7 padded to 4/8) went through ATOM's attention over an
        # EMPTY context and can be NaN/Inf.  The kernel is not NaN-tolerant across samples (one non-finite
        # row turns every real row's output NaN), and ATOM's per-row layers never notice.  Zero them.
        h = torch.nan_to_num(h, nan=0.0, posinf=0.0, neginf=0.0)
        md = get_forward_context().attn_metadata
        pos32 = positions.to(torch.int32)
        slot32 = md.slot_mapping[:S].to(torch.int32)
        indptr = md.kv_indptr[: S // Q + 1]  # one entry per SEQUENCE
        indices = md.kv_indices
        last = self.n_layers - 1
        for li in range(self.first, self.n_layers):
            op = self.by_sq[(S, Q)][li - self.first]
            if self.check and li == self.first and not torch.cuda.is_current_stream_capturing():
                h = self._checked_layer(li, op, positions, hs, residual, h, pos32, slot32, indptr, indices)
                continue
            h = op.forward_paged(
                h, pos32, self._pool(li), slot32, indptr, indices, self.cos, self.sin, layer=li - self.first,
                advance=(li == last),
            )
        # plain RMSNorm: ``h`` is already reduced (m.norm would all-reduce it again)
        x = h.float()
        y = x * torch.rsqrt(x.square().mean(-1, keepdim=True) + self.eps) * m.norm.weight.float()
        return y.to(h.dtype)

    # ------------------------------------------------------------- MTP draft
    def attach_draft(self, draft_model) -> None:
        """Route the draft model's MTP layer through the kernel (call once the drafter is built)."""
        if not self.has_draft:
            return
        predictor = draft_model.model
        for idx, layer in predictor.layers.items():
            name = f"dsv3_mega_draft_{idx}"
            _DRAFT_LAYERS[name] = (self, layer)
            layer._dsv3_mega_draft_name = name
        logger.info("[dsv3-mega] MTP draft layer attached (%d kernel variants)", len(self.draft_by_sq))

    def _draft_pool(self, layer) -> torch.Tensor:
        pool = self._kv_pools.get(("draft", id(layer)))
        if pool is None:
            kv = next(
                (
                    kc
                    for mod in layer.mtp_block.self_attn.modules()
                    if isinstance(kc := getattr(mod, "kv_cache", None), torch.Tensor) and kc.dim() == 3
                ),
                None,
            )
            if kv is None:  # engine warmup runs before the KV cache is allocated
                return None
            want = torch.float8_e4m3fn if self.kv_fp8 else torch.bfloat16
            assert kv.dtype == want and kv.shape[-1] == KV_LORA + PE_DIM, (kv.dtype, kv.shape)
            pool = self._kv_pools[("draft", id(layer))] = kv.view(-1, KV_LORA + PE_DIM)
        return pool

    def draft_layer(self, layer, h: torch.Tensor, positions: torch.Tensor):
        """Draft-model decoder layer (``mtp_block``) + ``shared_head.norm`` on the kernel.

        ``h`` is the ``eh_proj`` output.  Returns the post-shared_head-norm hidden, or None when this step
        is not covered (prefill / unsupported shape) so the caller runs ATOM's own layer."""
        ctx = get_forward_context()
        c, md = ctx.context, ctx.attn_metadata
        if c is None or c.is_prefill or md is None or md.kv_indptr is None or md.kv_indices is None:
            return None
        if md.slot_mapping is None:
            return None
        S = positions.shape[0]
        Q = int(md.max_seqlen_q) or 1
        op = self.draft_by_sq.get((S, Q))
        key = (S, Q, torch.cuda.is_current_stream_capturing())
        if key not in self.draft_stats:
            self.draft_stats[key] = 1
            if self.rank == 0:
                logger.info(
                    "[dsv3-mega][draft] S=%d Q=%d capturing=%s covered=%s scheduled_bs=%s running_bs=%s",
                    S, Q, key[2], op is not None, getattr(c, "scheduled_bs", None), getattr(c, "running_bs", None),
                )
        if op is None:
            return None
        pool = self._draft_pool(layer)
        if pool is None:
            return None
        h = torch.nan_to_num(h, nan=0.0, posinf=0.0, neginf=0.0)  # padded rows, see forward()
        pos32 = positions.to(torch.int32)
        slot32 = md.slot_mapping[:S].to(torch.int32)
        indptr = md.kv_indptr[: S // Q + 1]
        out = op.forward_paged(
            h, pos32, pool, slot32, indptr, md.kv_indices, self.cos, self.sin,
            layer=self.n_layers - self.first, advance=True,
        )
        if self.check and not torch.cuda.is_current_stream_capturing():
            ref_hs, ref_res = layer.mtp_block(positions=positions, hidden_states=h, residual=None)
            if self.defer_ar:
                ref_hs = tensor_model_parallel_all_reduce(ref_hs)
            ref = ref_hs.float() + ref_res.float()
            torch.cuda.synchronize()
            rel = ((out.float() - ref).norm() / ref.norm()).item()
            logger.info("[dsv3-mega][draft][check] S=%d Q=%d rank %d: mega vs ATOM mtp_block rel_l2=%.3e", S, Q, self.rank, rel)
        norm = layer.shared_head.norm
        x = out.float()
        y = x * torch.rsqrt(x.square().mean(-1, keepdim=True) + self.eps) * norm.weight.float()
        return y.to(out.dtype)

    # ------------------------------------------------------------------ debug
    def _checked_layer(self, li, op, positions, hs, residual, h, pos32, slot32, indptr, indices):
        """Run the first mega layer AND ATOM's own layer on the same input; log their difference.

        Both write the same new-token KV row (idempotent); ATOM's result is discarded."""
        ref_hs, ref_res = self.model.layers[li](positions, hs, residual)
        if self.defer_ar:
            ref_hs = tensor_model_parallel_all_reduce(ref_hs)
        ref = ref_hs.float() + ref_res.float()
        out = op.forward_paged(
            h, pos32, self._pool(li), slot32, indptr, indices, self.cos, self.sin, layer=0, advance=False
        )
        torch.cuda.synchronize()
        rel = ((out.float() - ref).norm() / ref.norm()).item()
        kv_lens = (indptr[1:] - indptr[:-1]).tolist()
        logger.info(
            "[dsv3-mega][check] layer %d rank %d S=%d kv_len=%s: mega vs ATOM layer rel_l2=%.3e",
            li, self.rank, positions.shape[0], kv_lens, rel,
        )
        if self.rank == 0 and not getattr(self, "_dumped", False) and max(kv_lens) > 64:
            self._dumped = True
            n = kv_lens[0]
            logger.info(
                "[dsv3-mega][meta] pos=%s slot=%s indptr=%s idx[:6]=%s idx[-6:]=%s dtype(indices)=%s pool_rows=%d",
                pos32.tolist(), slot32.tolist(), indptr.tolist(), indices[:6].tolist(),
                indices[n - 6 : n].tolist(), indices.dtype, self._pool(li).shape[0],
            )
        return out


def maybe_build(lm, config):
    """Return a ``DSV3MegaKernel`` if enabled and applicable, else None (called once after weight load)."""
    if not envs.ATOM_DSV3_MEGAKERNEL:
        return None
    if getattr(config.hf_config, "model_type", "") != "deepseek_v3":
        logger.warning("[dsv3-mega] ATOM_DSV3_MEGAKERNEL=1 ignored: model_type is not deepseek_v3")
        return None
    return DSV3MegaKernel(lm, config)
