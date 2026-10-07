# SPDX-License-Identifier: MIT
"""DeepSeek-V4 decode through the FlyDSL megakernel (opt-in, ``ATOM_DSV4_MEGAKERNEL=1``).

Each covered decoder layer (attention with its compressors and CSA indexer, both mHC
mixes, MoE and the TP reductions) is one launch. Prefill, MTP draft steps and decode
steps of more than 8 tokens run ATOM's path unchanged.

The kernel works on ATOM's own state in place (the fp8 KV pool, ``swa_dest_rows``,
``kv_indices_{hca,csa}``, block tables, compressor rings, the CSA indexer's FP4 key
cache), so a sequence can switch paths between steps. The routed experts are ATOM's
FusedMoE tensors in its gfx950 MXFP4 layout (``ATOM_MOE_GU_ITLV=1``); the rest of a
layer (~0.1 GB per rank) is packed from the checkpoint, since ATOM post-processes those
matrices. Batches pad to 1/2/4/8 by repeating sample 0, whose outputs are dropped.
"""

from __future__ import annotations

import time

import torch

from atom.utils import envs
from atom.utils.forward_context import AttnState, get_forward_context

try:  # needs an aiter that ships the FlyDSL DeepSeek-V4 MonoKernel
    from aiter.ops.flydsl.kernels.dsv4_monokernel.checkpoint import (
        Checkpoint,
        load_expert,
        load_layer,
    )
    from aiter.ops.flydsl.kernels.dsv4_monokernel.config import MoeMode
    from aiter.ops.flydsl.kernels.dsv4_monokernel.op import Dsv4MonoKernel, Dsv4Variant
    from aiter.ops.flydsl.kernels.dsv4_monokernel.packing import (
        pack_mxfp4,
        pack_mxfp4_scales,
    )
except ImportError:  # pragma: no cover - reported when the feature is enabled
    Dsv4MonoKernel = None

from aiter.dist.parallel_state import get_tp_group

logger = __import__("logging").getLogger("atom")

SUPPORTED_S = (1, 2, 4, 8)
# Verify-step launches: the kernel allows 4, but each re-streams the layer's weights, so
# a multi-launch step is slower than ATOM's path and goes there instead.
MTP_MAX_LAUNCHES = 1
MTP_MAX_S = 8  # tokens per verify launch: the kernel's 8-sample ceiling


class DSV4MegaKernel:
    """Per-rank owner of the kernel layers; ``forward`` replaces the model body for covered decode steps."""

    def __init__(self, lm, config):
        if Dsv4MonoKernel is None:
            raise ImportError(
                "ATOM_DSV4_MEGAKERNEL=1 needs an aiter with aiter.ops.flydsl.kernels.dsv4_monokernel"
            )
        hf = config.hf_config
        self.model = lm.model
        self.n_layers = hf.num_hidden_layers
        k = envs.ATOM_DSV4_MEGAKERNEL_LAYERS
        self.layer_ids = list(range(min(k, self.n_layers) if k > 0 else self.n_layers))
        tp = get_tp_group()
        self.tp, self.rank, self.group = tp.world_size, tp.rank_in_group, tp.cpu_group
        self.dev = torch.device("cuda", torch.cuda.current_device())
        assert (
            config.kv_cache_dtype == "fp8"
        ), "the kernel reads and writes ATOM's fp8 KV layout: --kv_cache_dtype fp8"
        spec = getattr(config, "speculative_config", None)
        k_spec = (
            spec.num_speculative_tokens
            if spec is not None and spec.num_speculative_tokens
            else 0
        )
        if k_spec:
            assert (
                spec.method == "mtp"
            ), f"only MTP's rectangular verify step is covered, not {spec.method}"
        # with MTP a decode step verifies K + 1 tokens per sequence, as a run of samples
        self.tok = 1 + k_spec
        assert (
            self.tok in SUPPORTED_S
        ), f"num_speculative_tokens {k_spec}: runs must tile the 8-sample launch"
        self.max_s = MTP_MAX_S if self.tok > 1 else SUPPORTED_S[-1]
        self.sizes = tuple(
            self.tok * n for n in (1, 2, 4, 8, 16) if self.tok * n <= self.max_s
        )
        assert (
            not self.model.enable_res_preshuffle
        ), "the preshuffled mHC residual (gfx1250) is not covered"
        self.hc = hf.hc_mult
        self.max_seq = config.max_model_len
        self.calls: dict[int, int] = {}
        self._load_layers(config)
        # ATOM's own rope values (fp32 [positions, rope_dim / 2]), so decode rotates as prefill did
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
        self.table_of = [
            self.tables[id(self.model.layers[li].attn.rotary_emb.cos_cache)]
            for li in self.layer_ids
        ]

    # ------------------------------------------------------------------ setup
    def _load_layers(self, config):
        ck = Checkpoint(config.model)
        t0 = time.perf_counter()
        prev_threads = torch.get_num_threads()
        torch.set_num_threads(4)  # 8 ranks x all cores oversubscribe the node
        self.variants: dict[tuple[int, int], Dsv4Variant] = {}
        self.by_s: dict[int, list] = {S: [] for S in self.sizes}
        self.ratio_of = []
        for li in self.layer_ids:
            W = load_layer(
                ck, li, rank=self.rank, tp=self.tp, device=self.dev, experts=False
            )
            cfg = W.cfg
            experts = self._atom_experts(li, cfg)
            if li == self.layer_ids[0]:
                self._check_expert_layout(ck, li, experts)
            cfg.kv_fp8, cfg.max_seq = True, self.max_seq
            # ATOM's indexer rotates neither queries nor keys
            cfg.indexer_hadamard = False
            cfg.validate()
            self.ratio_of.append(cfg.compress_ratio)
            base = None
            for S in self.sizes:
                key = (cfg.compress_ratio, S)
                if key not in self.variants:
                    self.variants[key] = Dsv4Variant(
                        cfg,
                        S,
                        rank=self.rank,
                        npes=self.tp,
                        group=self.group,
                        moe_mode=MoeMode.A8W4,
                        tokens_per_seq=self.tok,
                    )
                op = Dsv4MonoKernel(
                    W,
                    S,
                    rank=self.rank,
                    npes=self.tp,
                    group=self.group,
                    moe_mode=MoeMode.A8W4,
                    variant=self.variants[key],
                    packed=None if base is None else base.packed,
                    experts=experts if base is None else None,
                    own_state=False,  # forward always passes ATOM's state (_state)
                )
                base = base or op
                self.by_s[S].append(op)
            # the raw copies of what was packed are dead weight
            for name in base.packed:
                W.t.pop(name, None)
            torch.cuda.synchronize()
            if self.rank == 0 and li % 8 == 0:
                logger.info(
                    "[dsv4-mega] layer %d/%d loaded (%.0fs)",
                    li,
                    len(self.layer_ids),
                    time.perf_counter() - t0,
                )
        torch.set_num_threads(prev_threads)
        logger.info(
            "[dsv4-mega] rank %d: layers %d..%d on the kernel, ready in %.0fs",
            self.rank,
            self.layer_ids[0],
            self.layer_ids[-1],
            time.perf_counter() - t0,
        )

    def _atom_experts(self, li, cfg):
        """Layer ``li``'s routed expert bank as ATOM holds it, viewed in the kernel's
        terms: no copy. ATOM's gfx950 MXFP4 prep (FlyDSL fused_moe branch) shuffles
        weights and E8M0 scales with aiter's GU-interleaved layout, which the kernel
        reads directly."""
        fm = self.model.layers[li].ffn.experts
        assert (
            envs.ATOM_MOE_GU_ITLV
        ), "the kernel reads ATOM's GU-interleaved expert layout: ATOM_MOE_GU_ITLV=1"
        assert getattr(fm.w13_weight, "is_shuffled", False) and getattr(
            fm.w2_weight, "is_shuffled", False
        ), "expected ATOM's shuffled (FlyDSL fused_moe) MXFP4 expert layout"
        e, inter, hidden = cfg.n_experts, cfg.inter, cfg.hidden
        pad8 = lambda n: -(-n // 8) * 8
        bank = {
            "w_ug": fm.w13_weight.data.view(torch.uint8).view(
                e, 2 * inter, hidden // 2
            ),
            "s_ug": fm.w13_weight_scale.data.view(torch.uint8).view(
                e, 2 * inter, pad8(hidden // 32)
            ),
            "w_dn": fm.w2_weight.data.view(torch.uint8).view(e, hidden, inter // 2),
            "s_dn": fm.w2_weight_scale.data.view(torch.uint8).view(
                e, hidden, pad8(inter // 32)
            ),
        }
        for name, t in bank.items():
            assert (
                t.is_contiguous()
            ), f"layer {li} {name}: ATOM's expert tensor is not contiguous"
        return bank

    def _check_expert_layout(self, ck, li, bank):
        """Expert 0 of layer ``li`` from the checkpoint, laid out by the kernel's packer,
        must equal ATOM's bytes: a layout ATOM changes underneath fails here, at load,
        instead of as wrong output."""
        ug, ugs, dn, dns = load_expert(ck, li, 0, self.rank, self.tp, self.dev)
        want = {
            "w_ug": pack_mxfp4(ug[None], True)[0],
            "s_ug": pack_mxfp4_scales(ugs[None], True)[0],
            "w_dn": pack_mxfp4(dn[None], False)[0],
            "s_dn": pack_mxfp4_scales(dns[None], False)[0],
        }
        for name, w in want.items():
            assert torch.equal(
                bank[name][0], w
            ), f"layer {li} expert 0 {name}: ATOM's expert layout is not the one the kernel reads"

    # --------------------------------------------------------------- dispatch
    def applies(self, positions: torch.Tensor) -> bool:
        ctx = get_forward_context()
        c = ctx.context
        if c is None or c.is_prefill or c.is_dummy_run or c.is_draft:
            return False
        md = ctx.attn_metadata
        if (
            md is None
            or getattr(md, "state", None) is not AttnState.DECODE
            or md.swa_dest_rows is None
        ):
            return False
        if (
            getattr(self.model.layers[self.layer_ids[0]].attn, "unified_kv", None)
            is None
        ):
            return False  # engine warmup runs before the KV cache is bound
        if self.tok > 1:
            # a verify step: whole runs of K + 1 tokens, up to MTP_MAX_LAUNCHES launches
            T = positions.shape[0]
            return (
                md.max_seqlen_q == self.tok
                and T % self.tok == 0
                and T <= self.max_s * MTP_MAX_LAUNCHES
            )
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
        st = {
            "kv_state": comp.kv_state,
            "score_state": comp.score_state,
            "state_slots": slots,
            "st_kv": comp.kv_state.stride(0),
        }
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
    def _forward_mtp(
        self, input_ids: torch.Tensor, positions: torch.Tensor
    ) -> torch.Tensor:
        """A verify step: T = running_bs * (K + 1) tokens, sequence i's at rows
        [i * (K + 1), (i + 1) * (K + 1)) with consecutive positions. Every per-token
        input is ATOM's own (positions, write rows, key lists); per-sequence ones
        (state slot, block table) are gathered through batch_id_per_q_token. The
        step runs as launches of whole sequences, at most 8 tokens each."""
        m = self.model
        md = get_forward_context().attn_metadata
        T = positions.shape[0]
        tok = self.tok
        S = min(T, self.max_s)
        G = T // S
        self.calls[T] = self.calls.get(T, 0) + 1
        if self.rank == 0 and self.calls[T] in (1, 10, 100, 1000, 10000):
            logger.info(
                "[dsv4-mega] MTP verify steps on the kernel (tokens: count): %s (capturing: %s)",
                self.calls,
                torch.cuda.is_current_stream_capturing(),
            )
        # CUDA-graph padding (batch id -1) becomes a copy of sequence 0's run (see _forward)
        bid = md.batch_id_per_q_token[:T]
        ar = torch.arange(T, device=self.dev)
        src = torch.where(bid >= 0, ar, ar % tok)
        seq = bid[src].long()
        self._pos = positions[:T].to(torch.int32).contiguous()
        pos = self._pos[src].contiguous()
        tokens = input_ids[:T].to(torch.int32)[src].contiguous()
        slots = md.state_slot_out[seq].to(torch.int32).contiguous()
        bt = md.block_tables[seq].to(torch.int32).contiguous()
        env = int(md.envelope_rows)
        ops = self.by_s[S]
        if not getattr(self, "_rings_checked", False):
            # ATOM widens the compressor rings by K under MTP, as the kernel does; the
            # states bind after load, so this is checked on the first verify step
            for i, li in enumerate(self.layer_ids):
                comp = getattr(m.layers[li].attn, "compressor", None)
                if comp is not None:
                    want = ops[i].W.cfg.c_rows + tok - 1
                    assert (
                        comp.kv_state.shape[1] == want
                    ), f"layer {li}: compressor state has {comp.kv_state.shape[1]} rows, the kernel's ring {want}"
            self._rings_checked = True
        rows = {}
        for i, li in enumerate(self.layer_ids):
            r = self.ratio_of[i]
            if r not in rows:
                cfg = ops[i].W.cfg
                dest = torch.stack(
                    [
                        md.swa_dest_rows[r][:T].to(torch.int32)[src],
                        torch.zeros(T, dtype=torch.int32, device=self.dev),
                    ]
                )
                rows[r] = (
                    dest.contiguous(),
                    self._index_rows(md, r, T, cfg.n_keys, cfg.window)[
                        src
                    ].contiguous(),
                )
        h = m.embed(input_ids[:T])[src]
        h = h.unsqueeze(1).repeat(1, self.hc, 1).contiguous()
        for i, li in enumerate(self.layer_ids):
            attn = m.layers[li].attn
            dest, idx = rows[self.ratio_of[i]]
            cos, sin = self.table_of[i]
            outs = []
            for g in range(G):
                sl = slice(g * S, (g + 1) * S)
                outs.append(
                    ops[i].forward(
                        h[sl].contiguous(),
                        pos[sl],
                        (attn.unified_kv.view(torch.uint8), attn.unified_kv_rope),
                        dest[:, sl].contiguous(),
                        idx[sl],
                        cos,
                        sin,
                        layer=li + g * self.n_layers,
                        advance=False,
                        tokens=tokens[sl],
                        block_tables=bt[sl],
                        env_rows=env,
                        state=self._state(li, slots[sl]),
                    )
                )
            h = outs[0] if G == 1 else torch.cat(outs)
        for (r, s), var in self.variants.items():
            if s == S:
                var.advance_step()
        if len(self.layer_ids) == self.n_layers:
            return h
        from atom.models.deepseek_v4 import HCState

        hc_state = HCState(
            residual=h, post_mix=None, comb_mix=None, x_prev=None, res_preshuffle=False
        )
        for li in range(len(self.layer_ids), self.n_layers):
            hc_state = m.layers[li](hc_state, positions)
        return m.layers[-1].hc_post(
            hc_state.x_prev, hc_state.residual, hc_state.post_mix, hc_state.comb_mix
        )

    def forward(self, input_ids: torch.Tensor, positions: torch.Tensor) -> torch.Tensor:
        if self.tok > 1:
            return self._forward_mtp(input_ids, positions)
        m = self.model
        md = get_forward_context().attn_metadata
        S = positions.shape[0]
        Sp = next(s for s in SUPPORTED_S if s >= S)
        self.calls[S] = self.calls.get(S, 0) + 1
        if self.rank == 0 and self.calls[S] in (1, 10, 100, 1000, 10000):
            logger.info(
                "[dsv4-mega] decode steps on the kernel: %s (capturing: %s)",
                self.calls,
                torch.cuda.is_current_stream_capturing(),
            )

        # CUDA-graph padding (batch id -1) has placeholder slots, rows and block tables
        # that ATOM's kernels skip. The kernel has no skip, so each padding entry becomes
        # a copy of sample 0 (rewriting sample 0's own values) instead of writing
        # through the placeholders into live sequences' state.
        bid = md.batch_id_per_q_token
        live = (
            bid[:S] >= 0
            if bid is not None
            else torch.ones(S, dtype=torch.bool, device=self.dev)
        )

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
                    [
                        md.swa_dest_rows[r][:S].to(torch.int32),
                        torch.zeros(S, dtype=torch.int32, device=self.dev),
                    ]
                )
                rows[r] = (
                    torch.stack([pad(dest[0]), pad(dest[1])]).contiguous(),
                    pad(
                        self._index_rows(md, r, S, cfg.n_keys, cfg.window)
                    ).contiguous(),
                )
        h = m.embed(input_ids)
        h = pad(h.unsqueeze(1).repeat(1, self.hc, 1)).contiguous()
        for i, li in enumerate(self.layer_ids):
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
        for (r, s), var in self.variants.items():
            if s == Sp:
                var.advance_step()
        h = h[:S]
        if len(self.layer_ids) == self.n_layers:
            return h
        # the rest on ATOM's own layers, from a fully applied residual: no deferred hc_post to carry
        from atom.models.deepseek_v4 import HCState

        hc_state = HCState(
            residual=h, post_mix=None, comb_mix=None, x_prev=None, res_preshuffle=False
        )
        for li in range(len(self.layer_ids), self.n_layers):
            hc_state = m.layers[li](hc_state, positions)
        return m.layers[-1].hc_post(
            hc_state.x_prev, hc_state.residual, hc_state.post_mix, hc_state.comb_mix
        )


def maybe_build(lm, config):
    """Return a ``DSV4MegaKernel`` if enabled and applicable, else None (called once after weight load)."""
    if not envs.ATOM_DSV4_MEGAKERNEL:
        return None
    if getattr(config.hf_config, "model_type", "") != "deepseek_v4":
        logger.warning(
            "[dsv4-mega] ATOM_DSV4_MEGAKERNEL=1 ignored: model_type is not deepseek_v4"
        )
        return None
    return DSV4MegaKernel(lm, config)
