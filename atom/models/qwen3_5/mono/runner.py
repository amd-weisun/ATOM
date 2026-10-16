# SPDX-License-Identifier: MIT
# Copyright (C) 2026, Advanced Micro Devices, Inc. All rights reserved.
"""One decode step of up to ``MAX_TOKENS`` rows on the Qwen3.5 layer kernel: every decoder layer one
launch (``op.Qwen35MonoKernel``), over ATOM's own weights and state.

State is ATOM's: the GDN conv / recurrent pools and their state slots, the paged FP8 KV cache with its
scales, block tables and slot mapping, read and written in place, so a sequence can switch paths
between steps. The weights are ATOM's tensors, read in place in the layouts aiter's ``shuffle_weight``
(16, 16) gives them: the FP8 checkpoint's matrices and block scales (the shared expert fused into the
bank as expert 512), or the MXFP4 checkpoint's E2M1 bank and E8M0 scales with its BF16 projections
and shared expert. Only small tensors are derived at install: the f32 Gemma gammas ``1 + w``, the
router in MFMA tiles, the GDN b | a projection.

A step launches at its own width (rows): a plain decode one row a request, an MTP verify ``T =
spec_steps + 1`` rows a request on ATOM's GDN state -- ReplaySSM by default (a checkpoint a request
and ``replay_cap`` records of (k, u, g) a v-head; ATOM advances the record cursor before the step),
or the snapshot state (``ATOM_ENABLE_REPLAYSSM=0``: a state slot a drafted token). A width's kernels
and buffers (scratch, peer memory) are built on its first step (``WidthBuilds``); the layers of one
kind (GDN, full attention) share them.
"""

import inspect
import logging

import torch
from aiter.dist.parallel_state import get_tp_group

from atom.models.qwen3_5.mono.config import MAX_TOKENS, Q35Config, is_full_attention
from atom.models.qwen3_5.mono.dispatch import final_norm
from atom.models.qwen3_5.mono.layout import router_rows
from atom.models.qwen3_5.mono.op import Qwen35MonoKernel, Qwen35Variant
from atom.models.qwen3_5.mono.packing import pack_bf16
from atom.models.qwen3_5.mono.weights import LayerWeights, rope_tables
from atom.mono.plan.execution import BLOCKS
from atom.mono.runtime.abi import KernelAbi
from atom.mono.runtime.consensus import MonoUnsupported, bind_agreed
from atom.mono.runtime.widths import WidthBuilds
from atom.utils import envs
from atom.utils.forward_context import get_forward_context

logger = logging.getLogger("atom")


def _zero_abi(launcher) -> KernelAbi:
    """A launcher's parameters but ``stream``: what a compile-only call zeroes."""
    fn = getattr(launcher, "func", None) or launcher._func
    params = tuple(inspect.signature(fn).parameters)
    assert params[-1] == "stream", params
    return KernelAbi(params[:-1])


def baseline_refusal(experts, rows: int = 16) -> str | None:
    """Why the original path's MXFP4 routed experts do not compute what the kernel does -- MXFP4 weights
    on MXFP4 activations (A4W4, aiter's per_1x32), gate / up not interleaved, in aiter's shuffled gfx950
    layout -- on a decode of up to ``rows`` rows, or None. Asked of aiter's own activation rule, so mono
    serves only a deployment whose decode it reproduces."""
    qm = experts.quant_method
    if getattr(qm, "use_triton", False) or getattr(qm, "use_triton_decode", False):
        return "routed experts on the Triton MoE path"
    if getattr(qm, "is_guinterleave", False) or getattr(qm, "fp8_activations", False):
        return "routed experts gate / up interleaved or on FP8 activations (the kernel runs A4W4, GGUU)"
    if not getattr(experts.w13_weight, "is_shuffled", False):
        return "routed experts not in aiter's shuffled layout"
    try:
        from aiter.fused_moe import resolve_activation_dtype
        from aiter.ops.flydsl.moe_common import GateMode
    except ImportError as e:
        return f"cannot tell the original path's activation format: {e}"
    for m in range(1, rows + 1):
        got = resolve_activation_dtype(
            qm.quant_type,
            experts.w13_weight.dtype,
            activation=experts.activation,
            gate_mode=GateMode.SEPARATED,
            M=m,
            hidden_dtype=torch.bfloat16,
            has_a1_scale=getattr(experts, "w13_input_scale", None) is not None,
        )
        if got != getattr(torch, "float4_e2m1fn_x2", None):
            return f"the original path's {m}-row decode runs the routed experts on {got}, not MXFP4"
    return None


def language_model(causal_lm):
    """The module holding ``embed_tokens``, ``layers`` and ``norm``."""
    for m in causal_lm.modules():
        if all(hasattr(m, a) for a in ("embed_tokens", "layers", "norm")):
            return m
    raise MonoUnsupported("no embed_tokens / layers / norm in the model")


class Qwen35MonoDecodeRunner:
    """Owns the per-layer weight views (bound at install, before the KV cache is sized) and, per
    launch width, the compiled kernels and their buffers.

    Construction is collective over the TP group: every rank binds and checks its shard, then all
    agree (``consensus.bind_agreed``), so a rank that refuses turns mono off on every rank.
    """

    def __init__(
        self, causal_lm, atom_config, spec_steps: int, replay_cap: int = 0
    ) -> None:
        tp = get_tp_group()
        self.group = tp.cpu_group
        self.tp, self.rank = tp.world_size, tp.rank_in_group
        # an MTP verify: T rows a request, the anchor and its drafted tokens
        self.T = spec_steps + 1 if spec_steps else 1
        # ReplaySSM's record buffer length (0: the snapshot GDN state)
        self.replay_cap = replay_cap
        self.max_seq = atom_config.max_model_len + 1
        bind_agreed(lambda: self._bind(causal_lm), self.group)
        self.widths = WidthBuilds(
            self._build_width, self._width_kernels, self.group, "Qwen3.5 mono"
        )

    # ------------------------------------------------------------------ setup
    def _bind(self, causal_lm) -> None:
        """This rank's checks and weight views; raises ``MonoUnsupported``."""
        self.lm = language_model(causal_lm)
        self.dev = torch.device("cuda", torch.cuda.current_device())
        cus = torch.cuda.get_device_properties(self.dev).multi_processor_count
        if cus != BLOCKS:
            raise MonoUnsupported(
                f"{cus} CUs: the kernel needs all {BLOCKS} CTAs co-resident"
            )
        # the checkpoint format, from the expert bank: FP8 (the shared expert fused into it) or MXFP4
        # (amd/Qwen3.5-397B-A17B-MXFP4: A4W4 experts, BF16 projections and shared expert)
        w13 = self.lm.layers[0].mlp.experts.w13_weight
        weights = (
            "mxfp4" if w13.dtype == getattr(torch, "float4_e2m1fn_x2", None) else "fp8"
        )
        self.cfg = Q35Config(
            tp=self.tp,
            rank=self.rank,
            weights=weights,
            qk_fusion=envs.ATOM_ENABLE_QK_NORM_ROPE_CACHE_QUANT_FUSION,
        )
        n = len(self.lm.layers)
        self.full = [is_full_attention(i) for i in range(n)]
        for i, layer in enumerate(self.lm.layers):
            want = "full_attention" if self.full[i] else "linear_attention"
            if layer.layer_type != want:
                raise MonoUnsupported(
                    f"layer {i} is {layer.layer_type}, the kernel's plan {want}"
                )
        # per layer: (LayerWeights (shape only), the kernel's tensors: ATOM's, viewed)
        self.layers = [
            (LayerWeights(self.cfg, self.full[i]), self._pack(i, layer))
            for i, layer in enumerate(self.lm.layers)
        ]
        self.layer_nums = [
            (layer.self_attn.attn if self.full[i] else layer.linear_attn.attn).layer_num
            for i, layer in enumerate(self.lm.layers)
        ]
        rope = rope_tables(
            next(
                l for i, l in enumerate(self.lm.layers) if self.full[i]
            ).self_attn.rotary_emb,
            self.cfg,
            self.max_seq - 1,  # positions < max_model_len
        )
        if rope is None:
            raise MonoUnsupported(
                f"the RoPE caches do not cover {self.max_seq - 1} positions"
            )
        self.cos, self.sin = rope
        logger.info("Qwen3.5 mono: rank %d bound %d layers", self.rank, n)

    def _pack(self, i, layer) -> dict:
        """The kernel's weight dict over ATOM's tensors of decoder layer ``i``."""
        cfg = self.cfg

        def f32(t):
            return t.detach().float().contiguous()

        mlp = layer.mlp
        ex = mlp.experts
        mx = cfg.weights == "mxfp4"
        if mx:
            why = baseline_refusal(ex)
            if why is not None:
                raise MonoUnsupported(f"layer {i}: {why}")
            if ex.w13_weight.shape[0] != cfg.n_experts or mlp.shared_expert is None:
                raise MonoUnsupported(
                    f"layer {i}: MXFP4 bank {tuple(ex.w13_weight.shape)} without a BF16 shared expert"
                )
        elif (
            ex.w13_weight.dtype != torch.float8_e4m3fn
            or ex.w13_weight.shape[0] != cfg.n_experts + 1
        ):
            raise MonoUnsupported(
                f"layer {i}: routed experts {ex.w13_weight.dtype} {tuple(ex.w13_weight.shape)}, the kernel's"
                f" FP8 bank of {cfg.n_experts} + the fused shared expert"
            )
        w_r = torch.zeros(
            router_rows(cfg.n_experts),
            cfg.hidden,
            dtype=torch.bfloat16,
            device=self.dev,
        )
        # 512 experts, then the shared-expert gate row (one merged linear in both formats)
        w_r[: cfg.n_experts + 1] = mlp.gate.weight
        p = {
            "g_in1": 1.0 + f32(layer.input_layernorm.weight),
            "g_post1": 1.0 + f32(layer.post_attention_layernorm.weight),
            "w_r": pack_bf16(w_r),
        }
        if mx:
            # ATOM's bank as it holds it: gfx950 E2M1 tiles and E8M0 scales, gate / up not interleaved
            E, inter, hidden = cfg.n_experts, cfg.local_inter, cfg.hidden

            def pad8(n):
                return -(-n // 8) * 8

            p.update(
                w_ug=ex.w13_weight.data.view(torch.uint8).view(
                    E, 2 * inter, hidden // 2
                ),
                s_ug=ex.w13_weight_scale.data.view(torch.uint8).view(
                    E, 2 * inter, pad8(hidden // 32)
                ),
                w_dn=ex.w2_weight.data.view(torch.uint8).view(E, hidden, inter // 2),
                s_dn=ex.w2_weight_scale.data.view(torch.uint8).view(
                    E, hidden, pad8(inter // 32)
                ),
                w_sh_ug=mlp.shared_expert.gate_up_proj.weight,
                w_sh_dn=mlp.shared_expert.down_proj.weight,
            )
        else:
            p.update(
                w_ug=ex.w13_weight,
                s_ug=ex.w13_weight_scale,
                w_dn=ex.w2_weight,
                s_dn=ex.w2_weight_scale,
            )
        # the BF16 projections of the MXFP4 checkpoint have no scales: a placeholder in their slot
        none = torch.zeros(1, dtype=torch.float32, device=self.dev)
        if self.full[i]:
            a = layer.self_attn
            p.update(
                w_qkvz=a.qkv_proj.weight,
                s_qkvz=none if mx else a.qkv_proj.weight_scale,
                g_qn1=1.0 + f32(a.q_norm.weight),
                g_kn1=1.0 + f32(a.k_norm.weight),
                w_out=a.o_proj.weight,
                s_out=none if mx else a.o_proj.weight_scale,
            )
        else:
            a = layer.linear_attn
            if not hasattr(a, "in_proj_qkvz") or not hasattr(a, "in_proj_ba"):
                raise MonoUnsupported(
                    f"layer {i}: GDN input projection not the qkvz + b | a pair"
                )
            p.update(
                w_qkvz=a.in_proj_qkvz.weight,
                s_qkvz=none if mx else a.in_proj_qkvz.weight_scale,
                w_ba=pack_bf16(a.in_proj_ba.weight.detach()),
                w_conv=a.conv1d.weight,
                a_log=f32(a.A_log),
                dt_bias=f32(a.dt_bias),
                g_norm=f32(a.norm.weight),
                w_out=a.out_proj.weight,
                s_out=none if mx else a.out_proj.weight_scale,
            )
        proj = torch.bfloat16 if mx else torch.float8_e4m3fn
        for k, t in p.items():
            if k in ("w_qkvz", "w_out"):
                want = proj
            elif k in ("w_ug", "w_dn", "s_ug", "s_dn") and mx:
                want = torch.uint8
            elif k in ("w_ug", "w_dn"):
                want = torch.float8_e4m3fn
            elif k.startswith(("s_", "g_", "a_log", "dt_")):
                want = torch.float32
            else:
                want = t.dtype  # the packed BF16 tiles and the raw conv weight
            if not t.is_contiguous() or t.dtype != want:
                raise MonoUnsupported(
                    f"layer {i} {k}: {t.dtype} {tuple(t.shape)}, not the kernel's format"
                )
        return p

    def _build_width(self, S: int) -> dict:
        """The S-row launch: a variant (kernel, scratch, peer memory) per layer kind and an op per
        layer. Collective (the peer handshakes): every rank builds the same widths in the same order.
        """
        T = self.T
        self._check_records()
        variants = {
            full: Qwen35Variant(
                self.cfg,
                S,
                full,
                self.rank,
                self.tp,
                self.group,
                tokens_per_seq=T,
                replay_cap=0 if full else self.replay_cap,
            )
            for full in (False, True)
        }
        ops = [
            Qwen35MonoKernel(
                W,
                S,
                self.rank,
                self.tp,
                variant=variants[W.full],
                packed=p,
                tokens_per_seq=T,
            )
            for W, p in self.layers
        ]
        # every variant's peer buffer zeroed before the ranks agree they hold the width
        torch.cuda.synchronize()
        return {"variants": variants, "ops": ops}

    def _check_records(self) -> None:
        """ReplaySSM's record buffers as the kernel reads them. They bind with the KV
        cache, so this is checked when a width is built (its first step)."""
        if not self.replay_cap:
            return
        kvd = get_forward_context().kv_cache_data
        want = (self.cfg.hv, self.replay_cap, self.cfg.linear_k_dim)
        for i, full in enumerate(self.full):
            if full:
                continue
            rk = getattr(kvd[f"layer_{self.layer_nums[i]}"], "replay_buf_k", None)
            if rk is None or tuple(rk.shape[1:]) != want:
                got = None if rk is None else tuple(rk.shape)
                raise MonoUnsupported(
                    f"layer {i}: ReplaySSM records {got}, the kernel's [slots, {want}]"
                )

    @staticmethod
    def _width_kernels(built: dict) -> list:
        out = []
        for var in built["variants"].values():
            out.append((var.launch, _zero_abi(var.launch)))
            out.append((var._advance, _zero_abi(var._advance)))
        return out

    def launch_width(self, rows: int) -> int | None:
        """The launch width of a ``rows``-row step, or None (not served): whole requests' rows."""
        return rows if 0 < rows <= MAX_TOKENS and rows % self.T == 0 else None

    def prepare(self, rows: int) -> bool:
        """Whether every rank holds the launch of a ``rows``-row step."""
        S = self.launch_width(rows)
        return S is not None and self.widths.prepare(S)

    # ---------------------------------------------------------------- forward
    def forward(self, input_ids: torch.Tensor, positions: torch.Tensor) -> torch.Tensor:
        """The final normed hidden states [rows, hidden], as the model returns."""
        pos, slots, bt, gdn = self.step_inputs(input_ids, positions)
        x = self._layers(self.lm.embed_tokens(input_ids).float(), pos, slots, bt, gdn)
        # the layers' f32 stream is what the original path's final (fused all-reduce) norm normalizes
        return final_norm(self.lm.norm, x)

    def step_inputs(self, input_ids: torch.Tensor, positions: torch.Tensor):
        """The step's (positions, cache slots, block tables, GDN state indices) as the kernel takes them."""
        fwd = get_forward_context()
        md = fwd.attn_metadata
        S = input_ids.numel()
        pos = (positions[0] if positions.dim() == 2 else positions)[:S].to(torch.int32)
        g = md.gdn_metadata
        sm = md.slot_mapping
        if sm.numel() < S:
            # GDN's verify-graph capture metadata slices the per-token slot buffer to the sequence
            # count (gdn_attn.build_for_cudagraph_capture); the buffer itself holds every token's slot
            assert (
                sm.is_contiguous()
                and sm.untyped_storage().nbytes()
                >= (sm.storage_offset() + S) * sm.element_size()
            )
            sm = sm.as_strided((S,), (1,))
        slots = sm[:S].to(torch.int32)
        T = self.T
        if T == 1:
            gdn = (
                g.non_spec_state_indices_in_tensor[:S].to(torch.int32),
                g.non_spec_state_indices_tensor[:S].to(torch.int32),
            )
            bt = md.block_tables[:S]
        else:
            Q = S // T
            spec = g.spec_state_indices_tensor[:Q]
            n_acc = g.num_accepted_tokens[:Q].to(torch.int32)
            if self.replay_cap:
                # the conv window in each request's slot; the state its checkpoint + records
                conv_rows = spec[:, 0].to(torch.int32).repeat_interleave(T)
                gdn = (n_acc, conv_rows, g.slot_idx[:Q].to(torch.int32), g.write_pos)
            else:
                gdn = (n_acc, spec.reshape(-1).to(torch.int32))
            bt = md.block_tables[:Q].repeat_interleave(T, 0)
        return pos, slots, bt, gdn

    def _layers(self, x, pos, slots, bt, gdn):
        """The step's rows through every layer: one launch each."""
        kvd = get_forward_context().kv_cache_data
        built = self.widths[x.shape[0]]
        for i, op in enumerate(built["ops"]):
            lc = kvd[f"layer_{self.layer_nums[i]}"]
            if op.full:
                kv = (lc.k_cache, lc.v_cache, lc.k_scale, lc.v_scale, pos, bt, slots)
                x = op.forward(
                    x, kv=kv, rope=(self.cos, self.sin), layer=i, advance=False
                )
            else:
                state = (lc.k_cache, lc.v_cache) + gdn
                if self.replay_cap:
                    state += (lc.replay_buf_k, lc.replay_buf_u, lc.replay_buf_g)
                x = op.forward(x, gdn_state=state, layer=i, advance=False)
        for var in built["variants"].values():
            var.advance_step()
        return x
