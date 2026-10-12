# SPDX-License-Identifier: MIT
"""Bench-only (qwen_mono_bench): Qwen3.8-Flash-Next decode on the FlyDSL MonoKernel, one launch per layer (TP1).

``QWEN_MONO=1`` wraps the loaded model. A decode step of at most ``MAX_ROWS`` rows runs embed -> 48 layer
launches on the hyper-connection bundle (layers.1's PLE on ATOM between launches) -> the final mixer (or, with
a drafter, the bundle itself: the mixer runs in compute_logits); so does an MTP verify step (mtp_k + 1 rows per
sequence, the snapshot GDN state: ATOM_ENABLE_REPLAYSSM=0). Every other forward is the original model's; the
draft layer stays on ATOM. State and weights are ATOM's own: GDN conv / fp32 ssm pools and slots,
the QSA pool (K / V, raw and compressed index keys) with block tables and slot mapping, every BF16 / FP8
matrix in place. Each launch completes its layer's combines (no deferred pending combine).
"""

import logging
import os

import torch
from torch import nn

logger = logging.getLogger("atom")
MAX_ROWS = 8


class _Runner:
    def __init__(self, model, atom_config, mtp_k=0):
        from kernels.monokernel.qwen38.config import Q38Config, is_full_attention
        from kernels.monokernel.qwen38.op import Qwen38MonoKernel, Qwen38Variant, rope_tables
        from kernels.monokernel.qwen38.reference import LayerWeights

        self.lm = model.model
        self.cfg = Q38Config()
        n = len(self.lm.layers)
        self.full = [is_full_attention(i) for i in range(n)]
        self.packed = [self._pack(layer, self.full[i]) for i, layer in enumerate(self.lm.layers)]
        max_seq = atom_config.max_model_len + 1
        self.cos, self.sin = rope_tables(self.cfg, max_seq)
        self.T_spec = mtp_k + 1 if mtp_k else 0
        self.variants, self.layers = {}, {}
        for T in (self.T_spec,) if mtp_k else (1,):  # under MTP the conv / PLE windows are the verify window
            for S in range(T, MAX_ROWS + 1, T):
                for full in (False, True):
                    self.variants[(S, full, T)] = Qwen38Variant(self.cfg, S, full, max_seq=max_seq, tokens_per_seq=T)
                self.layers[(S, T)] = [
                    Qwen38MonoKernel(
                        LayerWeights(self.cfg, self.full[i], {}),
                        S,
                        variant=self.variants[(S, self.full[i], T)],
                        packed=p,
                        tokens_per_seq=T,
                    )
                    for i, p in enumerate(self.packed)
                ]
        self._warm()
        logger.info("Qwen3.8 mono decode ready: %d layers", n)

    @staticmethod
    def _pack(layer, full):
        """The kernel's weight dict over ATOM's tensors of one decoder layer."""
        f32 = lambda t: t.detach().float().contiguous()  # noqa: E731
        p = {}
        for side, hc in (("hca", layer.attn_hyper_connection), ("hcm", layer.mlp_hyper_connection)):
            p[f"{side}_g"] = 1.0 + f32(hc.hc_norm.weight)
            p[f"{side}_down"] = hc.input_mix_weight_down.weight
            p[f"{side}_up"] = hc.input_mix_weight_up.weight
            p[f"{side}_inj"] = hc.block_inject_weight.weight
        if full:
            a = layer.self_attn
            p.update(
                w_qkv=a.qkv_proj.weight,
                w_idx=a.indexer.index_qk_proj.weight,
                w_out=a.o_proj.weight,
                g_qn1=1.0 + f32(a.q_norm.weight),
                g_kn1=1.0 + f32(a.k_norm.weight),
                g_iq1=1.0 + f32(a.indexer.q_layernorm.weight),
                g_ik1=1.0 + f32(a.indexer.k_layernorm.weight),
            )
        else:
            a = layer.linear_attn
            p.update(
                w_proj=a.in_proj_qkvzba.weight,
                w_conv=a.conv1d.weight,
                a_log=f32(a.A_log),
                dt_bias=f32(a.dt_bias),
                g_norm=f32(a.norm.weight),
                w_out=a.out_proj.weight,
            )
        m = layer.mlp
        p.update(
            w_r=m.gate.weight,
            w_ug=m.experts.w13_weight,
            s_ug=m.experts.w13_weight_scale,
            w_dn=m.experts.w2_weight,
            s_dn=m.experts.w2_weight_scale,
            w_sh_ug=m.shared_expert.gate_up_proj.weight,
            w_sh_dn=m.shared_expert.down_proj.weight,
        )
        for k, t in p.items():
            if not t.is_contiguous():
                raise RuntimeError(f"ATOM tensor {k} is not contiguous: {tuple(t.shape)} {t.stride()}")
        return p

    def _warm(self):
        """Compile every variant with a dry launch (no state written: every slot is -1)."""
        cfg, dev = self.cfg, "cuda"
        ssm = torch.zeros(1, cfg.linear_v_heads, 128, 128, dtype=torch.float32, device=dev)
        kc = torch.zeros(1, 16, cfg.kv_heads, cfg.head_dim, dtype=torch.bfloat16, device=dev)
        ic = torch.zeros(1, 16, 1, cfg.index_dim, dtype=torch.bfloat16, device=dev)
        cc = torch.zeros(1, 4, 1, cfg.index_dim, dtype=torch.bfloat16, device=dev)
        for (S, T), layers in self.layers.items():
            conv = torch.zeros(1, 3 if T == 1 else T + 2, cfg.conv_dim, dtype=torch.bfloat16, device=dev)
            neg = torch.full((S,), -1, dtype=torch.int32, device=dev)
            pos = torch.arange(S, dtype=torch.int32, device=dev) % T
            bt = torch.zeros(S, 1, dtype=torch.int32, device=dev)
            X = torch.zeros(S, cfg.streams, dtype=torch.bfloat16, device=dev)
            gdn = (conv, ssm, neg, neg) if T == 1 else (conv, ssm, neg[: S // T], neg)
            done = set()
            for i, L in enumerate(layers):
                if L.full in done:
                    continue
                done.add(L.full)
                if L.full:
                    L.forward(X, kv=(kc, kc, ic, cc, pos, bt, neg), rope=(self.cos, self.sin), layer=i, advance=False)
                else:
                    L.forward(X, gdn_state=gdn, layer=i, advance=False)
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
            # verify-graph capture metadata slices the per-token slot buffer to the sequence count; the buffer
            # itself holds every token's slot
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
        X = self.lm.embed_tokens(input_ids).repeat(1, self.cfg.hc).contiguous()
        for i, (L, layer) in enumerate(zip(self.layers[(S, T)], self.lm.layers)):
            if layer.ple is not None:
                X = (X + layer.ple.forward_with_state(X, input_ids, md.ple_metadata)).contiguous()
            if L.full:
                a = layer.self_attn  # the QSA module owns its paged caches
                kv = (a.k_cache, a.v_cache, a.raw_key_cache, a.compressed_key_cache, pos, bt, slots)
                X = L.forward(X, kv=kv, rope=(self.cos, self.sin), layer=i, advance=False)
            else:
                lc = kvd[f"layer_{layer.linear_attn.attn.layer_num}"]
                X = L.forward(X, gdn_state=(lc.k_cache, lc.v_cache) + gdn, layer=i, advance=False)
        for full in (False, True):
            self.variants[(S, full, T)].advance_step()
        if self.lm.return_hc_state:  # a drafter consumes the bundle; the mixer runs in compute_logits
            return X.view(-1, self.cfg.hc, self.cfg.hidden)
        mixed, _ = self.lm.hyper_connection_mixer.mix_fused(X, None)
        return mixed


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


def install_mono_decode(model, atom_config, drafter, metadata_builder):
    from atom.models.qwen3_5_mono import _dump_weights

    if os.environ.get("QWEN_DUMP_WEIGHTS"):
        _dump_weights(model, os.environ["QWEN_DUMP_WEIGHTS"])
    if os.environ.get("QWEN_MONO") != "1":
        return model
    from atom.utils import envs

    mtp_k = 0 if drafter is None else drafter.mtp_k
    if mtp_k and envs.ATOM_ENABLE_REPLAYSSM is not False:
        raise RuntimeError("Qwen3.8 mono decode under MTP needs the snapshot GDN state: ATOM_ENABLE_REPLAYSSM=0")
    return MonoDecodeModel(model, atom_config, mtp_k)
