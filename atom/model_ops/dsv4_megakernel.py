# SPDX-License-Identifier: MIT
"""DeepSeek-V4 decode through the FlyDSL megakernel (opt-in, ``ATOM_DSV4_MEGAKERNEL=1``).

Each covered decoder layer -- attention with its compressors and CSA indexer, both
hyper-connection mixes, MoE, and the TP reductions -- is one kernel launch. Prefill,
and any decode step the kernel does not cover (more than 8 sequences, MTP), run
ATOM's normal path unchanged.

The kernel reads and writes ATOM's OWN per-sequence state in place, so a sequence can
move between the two paths from one step to the next:

- KV rows in the unified fp8 pool (``--kv_cache_dtype fp8``): the layer's NoPE plane
  view (448 FP8 + E8M0 scales per 512-byte row) and its bf16 RoPE plane view;
- the window row each token writes (``swa_dest_rows``) and the rows it attends
  (``kv_indices_{hca,csa}``: this module turns the CSR lists into the kernel's
  fixed-width index rows, once per step per compress class);
- compressed entries through ``block_tables`` / ``envelope_rows``;
- compressor rings (``kv_state`` / ``score_state``, per state slot), and the CSA
  indexer's paged FP4 key cache.

It keeps its own packed copy of the weights, loaded from the checkpoint
(``kernels.dsv4_moe_layer.checkpoint``): ATOM's loaded parameters are post-processed
for its own kernels. ``ATOM_DSV4_MEGAKERNEL_LAYERS=K`` puts only the first K layers
on the kernel -- a memory budget, or a way to bisect.

Batches pad to 1/2/4/8 by repeating sample 0: the repeat writes the same bytes to the
same state as the original, and its outputs are dropped.
"""

from __future__ import annotations

import hashlib
import inspect
import os
import time

import torch

from atom.utils import envs
from atom.utils.forward_context import AttnState, get_forward_context

try:  # the kernels live in the FlyDSL checkout (``kernels`` package)
    import kernels.dsv4_moe_layer.checkpoint as _ckmod
    import kernels.dsv4_moe_layer.packing as _dsv4_packing
    import kernels.mla_moe_layer.packing as _mla_packing
    from kernels.dsv4_moe_layer.checkpoint import Checkpoint, config_for_layer, load_layer
    from kernels.dsv4_moe_layer.config import MoeMode
    from kernels.dsv4_moe_layer.layer import Dsv4MoeLayer, Dsv4Variant
    from kernels.dsv4_moe_layer.reference import LayerWeights
except ImportError:  # pragma: no cover - reported when the feature is enabled
    Dsv4MoeLayer = None

from aiter.dist.parallel_state import get_tp_group

logger = __import__("logging").getLogger("atom")

SUPPORTED_S = (1, 2, 4, 8)


def _cache_dir(config, tp: int, rank: int) -> str | None:
    """Node-local cache of this rank's PACKED layer weights (``ATOM_DSV4_MEGAKERNEL_CACHE``, "" disables).

    A repeat launch then reads each layer's packed tensors from local disk instead of
    doing the strided checkpoint reads and the packing -- 5-6 min on ranks 1-7 and
    20 min on rank 0 cold. The key hashes everything the packed bytes depend on (the
    loader and both packing modules), so changing any of them starts a fresh cache."""
    root = envs.ATOM_DSV4_MEGAKERNEL_CACHE
    if not root:
        return None
    h = hashlib.sha256()
    for mod in (_ckmod, _dsv4_packing, _mla_packing):
        h.update(inspect.getsource(mod).encode())
    h.update(f"{config.model}|tp{tp}".encode())
    name = f"{os.path.basename(config.model.rstrip('/'))}_tp{tp}_{h.hexdigest()[:12]}"
    d = os.path.join(root, name, f"rank{rank}")
    os.makedirs(d, exist_ok=True)
    return d


def _save_layer(path: str, W, packed: dict) -> None:
    from safetensors.torch import save_file

    tensors = {f"t.{k}": v.contiguous().cpu() for k, v in W.t.items()}
    tensors.update({f"p.{k}": v.contiguous().cpu() for k, v in packed.items()})
    tmp = f"{path}.tmp{os.getpid()}"
    save_file(tensors, tmp)
    os.replace(tmp, path)  # atomic: a killed save never leaves a half file under the real name


def _load_cached_layer(path: str, cfg, dev):
    from safetensors.torch import load_file

    raw = load_file(path, device=str(dev))
    t = {k[2:]: v for k, v in raw.items() if k.startswith("t.")}
    packed = {k[2:]: v for k, v in raw.items() if k.startswith("p.")}
    return LayerWeights(cfg, t), packed


class DSV4MegaKernel:
    """Per-rank owner of the kernel layers; ``forward`` replaces the model body for covered decode steps."""

    def __init__(self, lm, config):
        if Dsv4MoeLayer is None:
            raise ImportError("ATOM_DSV4_MEGAKERNEL=1 needs the FlyDSL checkout on PYTHONPATH (package `kernels`)")
        hf = config.hf_config
        self.model = lm.model
        self.n_layers = hf.num_hidden_layers
        k = envs.ATOM_DSV4_MEGAKERNEL_LAYERS
        self.layer_ids = list(range(min(k, self.n_layers) if k > 0 else self.n_layers))
        tp = get_tp_group()
        self.tp, self.rank, self.group = tp.world_size, tp.rank_in_group, tp.cpu_group
        self.dev = torch.device("cuda", torch.cuda.current_device())
        assert config.kv_cache_dtype == "fp8", "the kernel reads and writes ATOM's fp8 KV layout: --kv_cache_dtype fp8"
        spec = getattr(config, "speculative_config", None)
        assert spec is None or not spec.num_speculative_tokens, "MTP is not covered yet"
        assert not self.model.enable_res_preshuffle, "the preshuffled mHC residual (gfx1250) is not covered"
        self.hc = hf.hc_mult
        self.max_seq = config.max_model_len
        self.calls: dict[int, int] = {}
        self.check = envs.ATOM_DSV4_MEGAKERNEL_CHECK
        self._load_layers(config)
        # One table per rope parameter set, shared the way ATOM shares it; the kernel
        # takes fp32 [positions, rope_dim / 2]. ATOM's own bf16 values, so decode
        # rotates exactly as its prefill did.
        self.tables = {}
        for li in self.layer_ids:
            rot = self.model.layers[li].attn.rotary_emb
            key = id(rot.cos_cache)
            if key not in self.tables:
                n = rot.cos_cache.shape[0]
                self.tables[key] = (
                    rot.cos_cache.float().reshape(n, -1).contiguous(),
                    rot.sin_cache.float().reshape(n, -1).contiguous(),
                )
        self.table_of = [self.tables[id(self.model.layers[li].attn.rotary_emb.cos_cache)] for li in self.layer_ids]

    # ------------------------------------------------------------------ setup
    def _check_mid(self, li, im, xf):
        """Kernel's per-slot SwiGLU output vs an fp32 recomputation, from its own input and ATOM's."""
        from kernels.dsv4_moe_layer.checkpoint import Checkpoint

        if not hasattr(self, "_ck"):
            self._ck = Checkpoint(self.model_path)
        rel = lambda x, y: ((x.float() - y.float()).norm() / y.float().norm().clamp_min(1e-6)).item()
        mid, xq, sel = im["mid"][0], im["xq"][:1], im["sel"][0].tolist()
        inter = mid.shape[-1]
        out = []
        for slot, e in enumerate(sel):
            rk = _mid_ref(self._ck, li, e, self.rank, self.tp, inter, xq)[0]
            ra = _mid_ref(self._ck, li, e, self.rank, self.tp, inter, xf)[0]
            out.append("%d:%.3f/%.3f" % (e, rel(mid[slot], rk), rel(mid[slot], ra)))
        from kernels.dsv4_moe_layer.reference import quant_dequant_mxfp8

        xqa = quant_dequant_mxfp8(xf.float())
        logger.info("[dsv4-mega] XQ layer %d  mxfp8(atom x) vs atom x %.4f  kernel xq vs mxfp8(atom x) %.4f",
                    li, rel(xqa, xf), rel(xq, xqa))
        logger.info("[dsv4-mega] MID layer %d xq-vs-atom-x %.4f  slot e:kernel-vs-ref(own x)/(atom x) %s",
                    li, rel(xq, xf), " ".join(out))

    def _load_layers(self, config):
        self.model_path = config.model
        ck = Checkpoint(config.model)
        t0 = time.perf_counter()
        prev_threads = torch.get_num_threads()
        torch.set_num_threads(4)  # 8 ranks x all cores oversubscribe the node
        self.variants: dict[tuple[int, int], Dsv4Variant] = {}
        self.by_s: dict[int, list] = {S: [] for S in SUPPORTED_S}
        self.ratio_of = []
        cdir = _cache_dir(config, self.tp, self.rank)
        n_hit = 0
        for li in self.layer_ids:
            cpath = os.path.join(cdir, f"layer{li}.safetensors") if cdir else None
            packed0 = None
            if cpath is not None and os.path.exists(cpath):
                W, packed0 = _load_cached_layer(cpath, config_for_layer(config.model, li, self.tp), self.dev)
                n_hit += 1
            else:
                W = load_layer(ck, li, rank=self.rank, tp=self.tp, device=self.dev)
            cfg = W.cfg
            cfg.kv_fp8, cfg.max_seq = True, self.max_seq
            cfg.indexer_hadamard = False  # ATOM's indexer rotates neither queries nor keys
            cfg.validate()
            self.ratio_of.append(cfg.compress_ratio)
            base = None
            for S in SUPPORTED_S:
                key = (cfg.compress_ratio, S)
                if key not in self.variants:
                    self.variants[key] = Dsv4Variant(
                        cfg, S, rank=self.rank, npes=self.tp, group=self.group, moe_mode=MoeMode.A8W4,
                        allow_unindexed_csa=True,  # a stale guard: the indexer runs in-kernel
                    )
                op = Dsv4MoeLayer(
                    W, S, rank=self.rank, npes=self.tp, group=self.group, moe_mode=MoeMode.A8W4,
                    allow_unindexed_csa=True, variant=self.variants[key],
                    packed=packed0 if base is None else base.packed,
                )
                base = base or op
                self.by_s[S].append(op)
            for name in base.packed:  # the raw copies of what was packed are dead weight
                W.t.pop(name, None)
            if cpath is not None and packed0 is None:
                _save_layer(cpath, W, base.packed)
            torch.cuda.synchronize()
            if self.rank == 0 and li % 8 == 0:
                logger.info("[dsv4-mega] layer %d/%d loaded (%.0fs)", li, len(self.layer_ids), time.perf_counter() - t0)
        torch.set_num_threads(prev_threads)
        logger.info(
            "[dsv4-mega] rank %d: layers %d..%d on the kernel, ready in %.0fs (%d from the packed-weight cache)",
            self.rank, self.layer_ids[0], self.layer_ids[-1], time.perf_counter() - t0, n_hit,
        )

    # --------------------------------------------------------------- dispatch
    def applies(self, positions: torch.Tensor) -> bool:
        ctx = get_forward_context()
        c = ctx.context
        if c is None or c.is_prefill or c.is_dummy_run or c.is_draft:
            return False
        md = ctx.attn_metadata
        if md is None or getattr(md, "state", None) is not AttnState.DECODE or md.swa_dest_rows is None:
            return False
        if getattr(self.model.layers[self.layer_ids[0]].attn, "unified_kv", None) is None:
            return False  # engine warmup runs before the KV cache is bound
        return positions.shape[0] <= SUPPORTED_S[-1]

    # ---------------------------------------------------------------- inputs
    def _index_rows(self, md, ratio, S, n_keys, window):
        """The kernel's [S, n_keys] rows for one compress class: window rows in
        [0, window), then (HCA) the compressed entries. ATOM's per-token CSR slice
        is [compressed head | window tail]; a CSA head is the indexer's pick, which
        the kernel makes itself, so only its tail is read here."""
        indptr = md.kv_indptr_hca if ratio == 128 else md.kv_indptr_csa
        flat = md.kv_indices_hca if ratio == 128 else md.kv_indices_csa
        start, end = indptr[:S].long(), indptr[1 : S + 1].long()
        pos = self._pos[:S].long()
        nw = torch.clamp(pos + 1, max=window)
        head = end - start - nw
        out = torch.full((S, n_keys), -1, dtype=torch.int32, device=self.dev)
        j = torch.arange(window, device=self.dev)
        src = start[:, None] + head[:, None] + j[None]
        ok = j[None] < nw[:, None]
        out[:, :window] = torch.where(ok, flat[src.clamp(max=flat.numel() - 1)], -1)
        if ratio == 128:
            j = torch.arange(n_keys - window, device=self.dev)
            src = start[:, None] + j[None]
            ok = j[None] < head[:, None]
            out[:, window:] = torch.where(ok, flat[src.clamp(max=flat.numel() - 1)], -1)
        return out

    def _state(self, li, slots):
        attn = self.model.layers[li].attn
        comp = attn.compressor
        st = dict(kv_state=comp.kv_state, score_state=comp.score_state, state_slots=slots, st_kv=comp.kv_state.stride(0))
        if attn.indexer is not None:
            ic = attn.indexer.compressor
            st.update(
                i_kv_state=ic.kv_state,
                i_score_state=ic.score_state,
                st_i=ic.kv_state.stride(0),
                i_cache=attn.indexer.kv_cache,
                i_cache_s=attn.indexer.kv_scale,
                st_ic=0,  # the pool is shared: the physical block id alone places an entry
            )
        return st

    # ---------------------------------------------------------------- forward
    def forward(self, input_ids: torch.Tensor, positions: torch.Tensor) -> torch.Tensor:
        m = self.model
        md = get_forward_context().attn_metadata
        S = positions.shape[0]
        Sp = next(s for s in SUPPORTED_S if s >= S)
        self.calls[S] = self.calls.get(S, 0) + 1
        if self.rank == 0 and self.calls[S] in (1, 10, 100, 1000, 10000):
            logger.info(
                "[dsv4-mega] decode steps on the kernel: %s (capturing: %s)",
                self.calls, torch.cuda.is_current_stream_capturing(),
            )

        # A CUDA graph runs at its captured batch: the tail past the live sequences is
        # padding (batch id -1) that ATOM's own kernels skip, with placeholder slots,
        # rows and block tables. The kernel has no skip, so every padding entry is made
        # a copy of sample 0: it computes sample 0's values and writes them over
        # sample 0's own, which is harmless. Without this it wrote through the
        # placeholders into live sequences' KV and compressor state.
        bid = md.batch_id_per_q_token
        live = bid[:S] >= 0 if bid is not None else torch.ones(S, dtype=torch.bool, device=self.dev)

        def pad(t):
            t = torch.where(live.view(-1, *([1] * (t.dim() - 1))), t, t[:1])
            return torch.cat([t, t[:1].expand(Sp - S, *t.shape[1:])]) if Sp > S else t

        input_ids = pad(input_ids)[:S]
        self._pos = pad(positions.to(torch.int32)).contiguous()
        tokens = pad(input_ids.to(torch.int32)).contiguous()
        slots = pad(md.state_slot_out[:S].to(torch.int32)).contiguous()
        bt = pad(md.block_tables[:S].to(torch.int32)).contiguous()
        env = int(md.envelope_rows)
        ops = self.by_s[Sp]
        rows = {}
        for i, li in enumerate(self.layer_ids):
            r = self.ratio_of[i]
            if r not in rows:
                cfg = ops[i].W.cfg
                dest = torch.stack(
                    [md.swa_dest_rows[r][:S].to(torch.int32), torch.zeros(S, dtype=torch.int32, device=self.dev)]
                )
                rows[r] = (
                    torch.stack([pad(dest[0]), pad(dest[1])]).contiguous(),
                    pad(self._index_rows(md, r, S, cfg.n_keys, cfg.window)).contiguous(),
                )
        h = m.embed(input_ids)
        h = pad(h.unsqueeze(1).repeat(1, self.hc, 1)).contiguous()
        check = self.check and not torch.cuda.is_current_stream_capturing()
        if check:
            from atom.models.deepseek_v4 import HCState
        for i, li in enumerate(self.layer_ids):
            h_in = h
            attn = m.layers[li].attn
            dest, idx = rows[self.ratio_of[i]]
            cos, sin = self.table_of[i]
            h = ops[i].forward(
                h,
                self._pos,
                (attn.unified_kv.view(torch.uint8), attn.unified_kv_rope),
                dest,
                idx,
                cos,
                sin,
                layer=li,
                advance=False,
                tokens=tokens,
                block_tables=bt,
                env_rows=env,
                state=self._state(li, slots),
            )
            if check:
                # ATOM's own layer on the same input, after ours: it rewrites the
                # step's state with its own values, and the step continues on its
                # output, so every layer is judged on ATOM's trajectory. The
                # attention half is rebuilt piecewise too, against the kernel's
                # post-attention residual `a`, to split attention from MoE.
                def rel(x, y):
                    x, y = x.float(), y.float()
                    return ((x - y).norm() / y.norm().clamp_min(1e-6)).item()

                blk = m.layers[li]
                if self.rank == 0 and self.calls[S] == 1 and li == self.layer_ids[0]:
                    from kernels.dsv4_moe_layer.reference import decode_kv_fp8

                    r0 = [int(x) for x in idx[0].tolist() if x >= 0]
                    nope = attn.unified_kv.view(torch.uint8)
                    dec = decode_kv_fp8(nope[r0], attn.unified_kv_rope[r0], 512)
                    logger.info(
                        "[dsv4-mega] DBG layer %d: dest %s  idx(valid) %s  row norms %s  nope planes %s %s rope %s",
                        li, dest[:, 0].tolist(), r0, [round(float(v), 3) for v in dec.norm(dim=-1)],
                        tuple(attn.unified_kv.shape), attn.unified_kv.dtype, tuple(attn.unified_kv_rope.shape),
                    )
                    logger.info(
                        "[dsv4-mega] DBG hca indptr %s indices[:12] %s swa_dest %s envelope %s state_slot %s bt %s",
                        md.kv_indptr_hca[: S + 1].tolist(), md.kv_indices_hca[:12].tolist(),
                        md.swa_dest_rows[128][:S].tolist(), md.envelope_rows, md.state_slot_out[:S].tolist(),
                        md.block_tables[:S, :4].tolist(),
                    )
                got_a = ops[i].intermediates()["a"][:S]
                xa, post, comb = blk.hc_pre(
                    h_in[:S], blk.hc_attn_fn, blk.hc_attn_scale, blk.hc_attn_base, blk.attn_norm.weight, blk.norm_eps
                )
                a_ref = blk.hc_post(blk.attn(xa, positions), h_in[:S], post, comb)
                # MoE half alone, on the kernel's own post-attention residual; `delta`
                # judges the MoE contribution (out - a) rather than the whole residual
                xf, post_f, comb_f = blk.hc_pre(
                    got_a, blk.hc_ffn_fn, blk.hc_ffn_scale, blk.hc_ffn_base, blk.ffn_norm.weight, blk.norm_eps
                )
                y_ref = blk.ffn(xf)
                m_ref = blk.hc_post(y_ref, got_a, post_f, comb_f)
                sh = blk.ffn.shared_experts
                sh_note = "fused" if sh is None else "%.3g" % (sh(xf).float().norm() / y_ref.float().norm()).item()
                gate = blk.ffn.gate
                lg = gate(xf)
                lg = (lg[0] if isinstance(lg, tuple) else lg).float()
                sc_ref = torch.sqrt(torch.nn.functional.softplus(lg))
                if getattr(gate, "tid2eid", None) is not None:
                    ids_ref = gate.tid2eid[tokens[:S].long()].long()
                else:
                    ids_ref = (sc_ref + gate.e_score_correction_bias.float()).topk(6, dim=-1).indices
                im = ops[i].intermediates()
                if self.rank == 0:
                    logger.info(
                        "[dsv4-mega] check step %d layer %d: scores %.4f  atom ids %s  kernel prob %s  atom prob %s",
                        self.calls[S], li, rel(im["scores"][:S], sc_ref), sorted(ids_ref[0].tolist()),
                        [round(float(v), 3) for v in im["prob"][0]],
                        [round(float(v), 3) for v in (lambda w: w / w.sum())(sc_ref[0, ids_ref[0]])],
                    )
                if self.rank == 0 and self.calls[S] <= 3:
                    self._check_mid(li, im, xf[:1])
                if self.rank == 0:
                    logger.info(
                        "[dsv4-mega] check step %d layer %d: moe half %.4f  moe delta %.4f  |shared|/|ffn| %s  sel %s",
                        self.calls[S], li, rel(h[:S], m_ref), rel(h[:S] - got_a, m_ref - got_a), sh_note,
                        ops[i].intermediates()["sel"][:S].tolist(),
                    )
                st = blk(HCState(residual=h_in[:S], res_preshuffle=False), positions)
                ref = blk.hc_post(st.x_prev, st.residual, st.post_mix, st.comb_mix)
                if self.rank == 0:
                    logger.info(
                        "[dsv4-mega] check step %d layer %d (ratio %d): after attention %.4f  layer %.4f  pos %s",
                        self.calls[S], li, self.ratio_of[i], rel(got_a, a_ref), rel(h[:S], ref), positions.tolist(),
                    )
                h = pad(ref.to(h.dtype)).contiguous()
        for (r, s), var in self.variants.items():
            if s == Sp:
                var.advance_step()
        h = h[:S]
        if len(self.layer_ids) == self.n_layers:
            return h
        # the rest on ATOM's own layers, from a fully applied residual: no deferred hc_post to carry
        from atom.models.deepseek_v4 import HCState

        hc_state = HCState(residual=h, post_mix=None, comb_mix=None, x_prev=None, res_preshuffle=False)
        for li in range(len(self.layer_ids), self.n_layers):
            hc_state = m.layers[li](hc_state, positions)
        return m.layers[-1].hc_post(hc_state.x_prev, hc_state.residual, hc_state.post_mix, hc_state.comb_mix)


def _mid_ref(ck, li, e, rank, tp, inter, x):
    """fp32 SwiGLU(x) of expert ``e``'s (384 = shared) rows for this rank, from the checkpoint."""
    from kernels.common.mx_formats import dequantize_mxfp4
    from kernels.dsv4_moe_layer.checkpoint import e8m0_float
    from kernels.mla_moe_layer.reference import dequant

    rows = (inter * rank, inter * (rank + 1))
    if e == 384:
        sh = f"layers.{li}.ffn.shared_experts."

        def w(n):
            q = ck.get(sh + n + ".weight", rows).view(torch.float8_e4m3fn)
            sc = e8m0_float(ck.get(sh + n + ".scale", (rows[0] // 128, rows[1] // 128)))
            return dequant(q, sc, 128).float()

    else:
        x_ = f"layers.{li}.ffn.experts.{e}."

        def w(n):
            q = ck.get(x_ + n + ".weight", rows).view(torch.uint8)
            return dequantize_mxfp4(q, ck.get(x_ + n + ".scale", rows).view(torch.uint8)).float()

    g, u = x.float() @ w("w1").to(x.device).T, x.float() @ w("w3").to(x.device).T
    g, u = g.clamp(max=10.0), u.clamp(-10.0, 10.0)
    return torch.nn.functional.silu(g) * u


def maybe_build(lm, config):
    """Return a ``DSV4MegaKernel`` if enabled and applicable, else None (called once after weight load)."""
    if not envs.ATOM_DSV4_MEGAKERNEL:
        return None
    if getattr(config.hf_config, "model_type", "") != "deepseek_v4":
        logger.warning("[dsv4-mega] ATOM_DSV4_MEGAKERNEL=1 ignored: model_type is not deepseek_v4")
        return None
    return DSV4MegaKernel(lm, config)
