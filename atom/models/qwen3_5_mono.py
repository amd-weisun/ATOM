# SPDX-License-Identifier: MIT
"""Bench-only (qwen_mono_bench): Qwen3.5-MoE decode on the FlyDSL MonoKernel, one launch per layer.

``QWEN_MONO=1`` wraps the loaded model (``install_mono_decode``, registered in ``mono_decode_installers``).
A decode step of at most ``MAX_ROWS`` rows (a captured graph's padded width) runs embed -> 60 layer
launches -> final norm; so does an MTP verify step (``mtp_k + 1`` rows per sequence, the snapshot GDN
state path: ``ATOM_ENABLE_REPLAYSSM=0``); every other forward (prefill, larger batches) is the original
model's. The MTP draft layer stays on ATOM.

State and weights are ATOM's own: GDN conv / ssm pools and state slots, the paged FP8 KV cache with its
block tables and slot mapping; every FP8 matrix (projections, the 512 + 1 shared experts) and its block
scales are read in place (aiter ``shuffle_weight`` (16, 16), the kernel's layout). Only small tensors are
derived: the f32 Gemma gammas ``1 + w``, the router in MFMA tiles, the GDN b | a projection.
"""

import logging
import os

import torch
from torch import nn

from atom.utils import envs

logger = logging.getLogger("atom")
MAX_ROWS = 8


def _lm(model):
    """The module holding embed_tokens / layers / norm."""
    for m in model.modules():
        if all(hasattr(m, a) for a in ("embed_tokens", "layers", "norm")):
            return m
    raise RuntimeError("no embed_tokens / layers / norm in the model")


class _Runner:
    def __init__(self, model, atom_config, mtp_k=0):
        from aiter.dist.parallel_state import get_tp_group

        from kernels.monokernel.qwen35.config import Q35Config, is_full_attention
        from kernels.monokernel.qwen35.op import Qwen35MonoKernel, Qwen35Variant
        from kernels.monokernel.qwen35.reference import LayerWeights, rope_tables

        tp = get_tp_group()
        self.rank, self.npes, group = tp.rank_in_group, tp.world_size, tp.cpu_group
        self.lm = _lm(model)
        self.cfg = Q35Config(tp=self.npes, rank=self.rank)
        n = len(self.lm.layers)
        self.full = [is_full_attention(i) for i in range(n)]
        self.packed = [
            (LayerWeights(self.cfg, self.full[i], {}), self._pack(layer, self.full[i]))
            for i, layer in enumerate(self.lm.layers)
        ]
        self.cos, self.sin = rope_tables(self.cfg, atom_config.max_model_len + 1)
        self.T_spec = mtp_k + 1 if mtp_k else 0
        self.variants, self.layers = {}, {}
        for T in (self.T_spec,) if mtp_k else (1,):  # under MTP the conv pool is the verify window
            for S in range(T, MAX_ROWS + 1, T):
                for full in (False, True):
                    self.variants[(S, full, T)] = Qwen35Variant(
                        self.cfg, S, full, self.rank, self.npes, group, tokens_per_seq=T
                    )
                self.layers[(S, T)] = [
                    Qwen35MonoKernel(
                        W,
                        S,
                        self.rank,
                        self.npes,
                        variant=self.variants[(S, self.full[i], T)],
                        packed=p,
                        tokens_per_seq=T,
                    )
                    for i, (W, p) in enumerate(self.packed)
                ]
        self._layer_nums = [self._layer_num(layer) for layer in self.lm.layers]
        self._warm()
        logger.info("Qwen3.5 mono decode ready: %d layers, rank %d / %d", n, self.rank, self.npes)

    def _pack(self, layer, full):
        """The kernel's weight dict over ATOM's tensors of one decoder layer."""
        from kernels.monokernel.packing import pack_bf16
        from kernels.monokernel.qwen35.kernel.plan import router_rows

        cfg = self.cfg
        f32 = lambda t: t.detach().float().contiguous()  # noqa: E731
        mlp = layer.mlp
        w_r = torch.zeros(router_rows(cfg.n_experts), cfg.hidden, dtype=torch.bfloat16, device="cuda")
        w_r[: cfg.n_experts + 1] = mlp.gate.weight  # 512 experts, then the shared-expert gate row
        ex = mlp.experts
        if ex.w13_weight.shape[0] != cfg.n_experts + 1:
            raise RuntimeError("expected the shared expert fused as expert 512")
        p = dict(
            g_in1=1.0 + f32(layer.input_layernorm.weight),
            g_post1=1.0 + f32(layer.post_attention_layernorm.weight),
            w_r=pack_bf16(w_r),
            w_ug=ex.w13_weight,
            s_ug=ex.w13_weight_scale,
            w_dn=ex.w2_weight,
            s_dn=ex.w2_weight_scale,
        )
        if full:
            a = layer.self_attn
            p.update(
                w_qkvz=a.qkv_proj.weight,
                s_qkvz=a.qkv_proj.weight_scale,
                g_qn1=1.0 + f32(a.q_norm.weight),
                g_kn1=1.0 + f32(a.k_norm.weight),
                w_out=a.o_proj.weight,
                s_out=a.o_proj.weight_scale,
            )
        else:
            a = layer.linear_attn
            p.update(
                w_qkvz=a.in_proj_qkvz.weight,
                s_qkvz=a.in_proj_qkvz.weight_scale,
                w_ba=pack_bf16(a.in_proj_ba.weight.detach()),
                w_conv=a.conv1d.weight,
                a_log=f32(a.A_log),
                dt_bias=f32(a.dt_bias),
                g_norm=f32(a.norm.weight),
                w_out=a.out_proj.weight,
                s_out=a.out_proj.weight_scale,
            )
        for k, t in p.items():
            if not t.is_contiguous() or (t.dtype == torch.float32) != k.startswith(("s_", "g_", "a_log", "dt_")):
                raise RuntimeError(f"unexpected ATOM tensor for {k}: {t.dtype} {tuple(t.shape)}")
        return p

    @staticmethod
    def _layer_num(layer):
        mod = layer.linear_attn.attn if layer.layer_type == "linear_attention" else layer.self_attn.attn
        return mod.layer_num

    def _warm(self):
        """Compile every variant with a dry launch (no state written: every slot is -1)."""
        cfg, dev = self.cfg, "cuda"
        ssm = torch.zeros(1, cfg.hv, cfg.linear_k_dim, cfg.linear_v_dim, dtype=torch.bfloat16, device=dev)
        kc = torch.zeros(1, 1, cfg.head_dim // 16, 16, 16, dtype=torch.float8_e4m3fn, device=dev)
        vc = torch.zeros(1, 1, 1, cfg.head_dim, 16, dtype=torch.float8_e4m3fn, device=dev)
        sc = torch.zeros(1, 1, 16, dtype=torch.float32, device=dev)  # ATOM sets the default dtype to bf16
        for (S, T), layers in self.layers.items():
            conv = torch.zeros(1, 3 if T == 1 else T + 2, cfg.conv_dim, dtype=torch.bfloat16, device=dev)
            neg = torch.full((S,), -1, dtype=torch.int32, device=dev)
            pos = torch.arange(S, dtype=torch.int32, device=dev) % T
            bt = torch.zeros(S, 1, dtype=torch.int32, device=dev)
            h = torch.zeros(S, cfg.hidden, dtype=torch.bfloat16, device=dev)
            gdn = (conv, ssm, neg, neg) if T == 1 else (conv, ssm, neg[: S // T], neg)
            done = set()
            for i, L in enumerate(layers):
                if L.full in done:
                    continue
                done.add(L.full)
                if L.full:
                    L.forward(h, kv=(kc, vc, sc, sc, pos, bt, neg), rope=(self.cos, self.sin), layer=i, advance=False)
                else:
                    L.forward(h, gdn_state=gdn, layer=i, advance=False)
            for full in (False, True):
                self.variants[(S, full, T)].advance_step()
        torch.cuda.synchronize()

    def forward(self, input_ids, positions):
        from atom.utils.forward_context import get_forward_context

        fwd = get_forward_context()
        md, kvd = fwd.attn_metadata, fwd.kv_cache_data
        S = input_ids.numel()
        pos = (positions[0] if positions.dim() == 2 else positions)[:S].to(torch.int32)
        g = md.gdn_metadata
        sm = md.slot_mapping
        if sm.numel() < S:
            # GDN's verify-graph capture metadata slices the per-token slot buffer to the sequence count
            # (gdn_attn.build_for_cudagraph_capture); the buffer itself holds every token's slot
            assert sm.is_contiguous() and sm.untyped_storage().nbytes() >= (sm.storage_offset() + S) * sm.element_size()
            sm = sm.as_strided((S,), (1,))
        slots = sm[:S].to(torch.int32)
        if g.spec_sequence_masks is None:
            T = 1
            gdn = (g.non_spec_state_indices_in_tensor[:S].to(torch.int32), g.non_spec_state_indices_tensor[:S].to(torch.int32))
            bt = md.block_tables[:S]
        else:  # MTP verify: T rows per sequence
            T = self.T_spec
            Q = S // T
            spec = g.spec_state_indices_tensor[:Q]
            if spec.shape[1] != T:
                raise RuntimeError(f"expected {T} state slots per sequence (ATOM_ENABLE_REPLAYSSM=0), got {tuple(spec.shape)}")
            gdn = (g.num_accepted_tokens[:Q].to(torch.int32), spec.reshape(-1).to(torch.int32))
            bt = md.block_tables[:Q].repeat_interleave(T, 0)
        x = self.lm.embed_tokens(input_ids)
        for i, L in enumerate(self.layers[(S, T)]):
            lc = kvd[f"layer_{self._layer_nums[i]}"]
            if L.full:
                kv = (lc.k_cache, lc.v_cache, lc.k_scale, lc.v_scale, pos, bt, slots)
                x = L.forward(x, kv=kv, rope=(self.cos, self.sin), layer=i, advance=False)
            else:
                x = L.forward(x, gdn_state=(lc.k_cache, lc.v_cache) + gdn, layer=i, advance=False)
        for full in (False, True):
            self.variants[(S, full, T)].advance_step()
        out = self.lm.norm(x)
        return out[0] if isinstance(out, tuple) else out


def step_supported(input_ids, inputs_embeds, T_spec=0) -> bool:
    from atom.utils.forward_context import get_forward_context

    rows = input_ids.numel()
    if inputs_embeds is not None or not 0 < rows <= MAX_ROWS:
        return False
    fwd = get_forward_context()
    if fwd.context is None or fwd.context.is_prefill or getattr(fwd, "ubatch_slices", None) is not None:
        return False
    g = getattr(fwd.attn_metadata, "gdn_metadata", None)
    if g is None or g.num_prefills != 0:
        return False
    if g.spec_sequence_masks is None:
        return T_spec == 0
    return T_spec > 1 and g.num_decodes == 0 and rows % T_spec == 0


class MonoDecodeModel(nn.Module):
    def __init__(self, model, atom_config, mtp_k=0):
        super().__init__()
        self.model = model
        self._mono = _Runner(model, atom_config, mtp_k)

    def forward(self, input_ids, positions, intermediate_tensors=None, inputs_embeds=None, **kw):
        if step_supported(input_ids, inputs_embeds, self._mono.T_spec):
            return self._mono.forward(input_ids, positions)
        return self.model(input_ids, positions, intermediate_tensors, inputs_embeds, **kw)

    def compute_logits(self, hidden_states):
        return self.model.compute_logits(hidden_states)

    def __getattr__(self, name):
        try:
            return super().__getattr__(name)
        except AttributeError:
            return getattr(self.model, name)


def _dump_weights(model, out):
    """QWEN_DUMP_WEIGHTS=<dir>: every rank saves layers 0 / 3's parameters as ATOM holds them after loading
    (routed experts cut to the first 4) plus name / shape / dtype / stride of every tensor, for the layout study."""
    from aiter.dist.parallel_state import get_tp_group

    rank = get_tp_group().rank_in_group
    lm = _lm(model)
    meta, keep = [], {}
    for i in (0, 3):
        for n, t in list(lm.layers[i].named_parameters()) + list(lm.layers[i].named_buffers()):
            meta.append(f"L{i} {n} {tuple(t.shape)} {t.dtype} {t.stride()}")
            keep[f"L{i}.{n}"] = (t[:4] if "experts.w" in n else t).detach().cpu().clone()
    os.makedirs(out, exist_ok=True)
    torch.save(keep, f"{out}/r{rank}.pt")
    if rank == 0:
        open(f"{out}/meta.txt", "w").write("\n".join(meta))
    logger.info("Qwen3.5 weights dumped to %s", out)


def install_mono_decode(model, atom_config, drafter, metadata_builder):
    if os.environ.get("QWEN_DUMP_WEIGHTS"):
        _dump_weights(model, os.environ["QWEN_DUMP_WEIGHTS"])
    if os.environ.get("QWEN_MONO") != "1":
        return model
    mtp_k = 0 if drafter is None else drafter.mtp_k
    if mtp_k and envs.ATOM_ENABLE_REPLAYSSM is not False:
        raise RuntimeError("Qwen3.5 mono decode under MTP needs the snapshot GDN state: ATOM_ENABLE_REPLAYSSM=0")
    return MonoDecodeModel(model, atom_config, mtp_k)
