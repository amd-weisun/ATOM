# SPDX-License-Identifier: MIT
"""Bench-only (qwen_mono_bench): Qwen3.8-Flash-Next decode on the FlyDSL MonoKernel, one launch per layer (TP1).

``QWEN_MONO=1`` wraps the loaded model. A decode step of at most ``MAX_ROWS`` rows runs embed -> 48 layer
launches on the hyper-connection bundle (layers.1's PLE on ATOM between launches) -> the final mixer; every
other forward is the original model's. State and weights are ATOM's own: GDN conv / fp32 ssm pools and slots,
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
    def __init__(self, model, atom_config):
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
        self.variants, self.layers = {}, {}
        for S in range(1, MAX_ROWS + 1):
            for full in (False, True):
                self.variants[(S, full)] = Qwen38Variant(self.cfg, S, full, max_seq=max_seq)
            self.layers[S] = [
                Qwen38MonoKernel(
                    LayerWeights(self.cfg, self.full[i], {}), S, variant=self.variants[(S, self.full[i])], packed=p
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
        conv = torch.zeros(1, 3, cfg.conv_dim, dtype=torch.bfloat16, device=dev)
        ssm = torch.zeros(1, cfg.linear_v_heads, 128, 128, dtype=torch.float32, device=dev)
        kc = torch.zeros(1, 16, cfg.kv_heads, cfg.head_dim, dtype=torch.bfloat16, device=dev)
        ic = torch.zeros(1, 16, 1, cfg.index_dim, dtype=torch.bfloat16, device=dev)
        cc = torch.zeros(1, 4, 1, cfg.index_dim, dtype=torch.bfloat16, device=dev)
        for S in range(1, MAX_ROWS + 1):
            neg = torch.full((S,), -1, dtype=torch.int32, device=dev)
            zero = torch.zeros(S, dtype=torch.int32, device=dev)
            bt = torch.zeros(S, 1, dtype=torch.int32, device=dev)
            X = torch.zeros(S, cfg.streams, dtype=torch.bfloat16, device=dev)
            done = set()
            for i, L in enumerate(self.layers[S]):
                if L.full in done:
                    continue
                done.add(L.full)
                if L.full:
                    L.forward(X, kv=(kc, kc, ic, cc, zero, bt, neg), rope=(self.cos, self.sin), layer=i, advance=False)
                else:
                    L.forward(X, gdn_state=(conv, ssm, neg, neg), layer=i, advance=False)
            for full in (False, True):
                self.variants[(S, full)].advance_step()
        torch.cuda.synchronize()

    def forward(self, input_ids, positions):
        from atom.utils.forward_context import get_forward_context

        fwd = get_forward_context()
        md, kvd = fwd.attn_metadata, fwd.kv_cache_data
        S = input_ids.numel()
        pos = (positions[0] if positions.dim() == 2 else positions)[:S].to(torch.int32)
        g = md.gdn_metadata
        sl_in = g.non_spec_state_indices_in_tensor[:S].to(torch.int32)
        sl_out = g.non_spec_state_indices_tensor[:S].to(torch.int32)
        bt = md.block_tables[:S]
        slots = md.slot_mapping[:S].to(torch.int32)
        X = self.lm.embed_tokens(input_ids).repeat(1, self.cfg.hc).contiguous()
        for i, (L, layer) in enumerate(zip(self.layers[S], self.lm.layers)):
            if layer.ple is not None:
                X = (X + layer.ple.forward_with_state(X, input_ids, md.ple_metadata)).contiguous()
            if L.full:
                a = layer.self_attn  # the QSA module owns its paged caches
                kv = (a.k_cache, a.v_cache, a.raw_key_cache, a.compressed_key_cache, pos, bt, slots)
                X = L.forward(X, kv=kv, rope=(self.cos, self.sin), layer=i, advance=False)
            else:
                lc = kvd[f"layer_{layer.linear_attn.attn.layer_num}"]
                X = L.forward(X, gdn_state=(lc.k_cache, lc.v_cache, sl_in, sl_out), layer=i, advance=False)
        for full in (False, True):
            self.variants[(S, full)].advance_step()
        mixed, _ = self.lm.hyper_connection_mixer.mix_fused(X, None)
        return mixed


def step_supported(input_ids, inputs_embeds) -> bool:
    from atom.utils.forward_context import get_forward_context

    rows = input_ids.numel()
    if inputs_embeds is not None or not 0 < rows <= MAX_ROWS:
        return False
    fwd = get_forward_context()
    if fwd.context is None or fwd.context.is_prefill or getattr(fwd, "ubatch_slices", None) is not None:
        return False
    g = getattr(fwd.attn_metadata, "gdn_metadata", None)
    return g is not None and g.num_prefills == 0 and g.spec_sequence_masks is None


class MonoDecodeModel(nn.Module):
    def __init__(self, model, atom_config):
        super().__init__()
        self.model = model
        self._mono = _Runner(model, atom_config)

    def forward(self, input_ids, positions, intermediate_tensors=None, inputs_embeds=None, **kw):
        if step_supported(input_ids, inputs_embeds):
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
    if drafter is not None:
        logger.info("Qwen3.8 mono decode off: speculative decoding is not supported yet")
        return model
    return MonoDecodeModel(model, atom_config)
