# SPDX-License-Identifier: MIT
# Copyright (C) 2026, Advanced Micro Devices, Inc. All rights reserved.
"""Which deployments and which steps the Qwen3.8-Flash-Next mono decode takes."""

import json
import os
from types import SimpleNamespace

import pytest
import torch

pytest.importorskip("aiter")

from atom.models.qwen4_exp.mono import dispatch
from atom.models.qwen4_exp.mono.config import MAX_TOKENS
from atom.mono.runtime import deployment


class _Mode:
    def __init__(self, piecewise):
        self.piecewise = piecewise

    def requires_piecewise_compilation(self):
        return self.piecewise


# Qwen3.8-Flash-Next's text config: the shapes the kernel is built for
_HF = {
    "hidden_size": 2560,
    "hc_count": 4,
    "hc_lowrank": 320,
    "linear_num_key_heads": 16,
    "linear_num_value_heads": 48,
    "linear_key_head_dim": 128,
    "linear_value_head_dim": 128,
    "linear_conv_kernel_dim": 4,
    "num_attention_heads": 24,
    "num_key_value_heads": 2,
    "head_dim": 256,
    "indexer_n_heads": 4,
    "indexer_head_dim": 128,
    "indexer_budget": 2048,
    "indexer_compress_ratio": 4,
    "num_experts": 512,
    "num_experts_per_tok": 10,
    "moe_intermediate_size": 640,
    "shared_expert_intermediate_size": 640,
}


def _atom_config(**overrides):
    fields = {
        "tensor_parallel_size": 1,
        "enable_expert_parallel": False,
        "parallel_config": SimpleNamespace(data_parallel_size=1),
        "pipeline_parallel_size": 1,
        "decode_context_parallel_size": 1,
        "prefill_context_parallel_size": 1,
        "enable_tbo": False,
        "enable_tbo_decode": False,
        "compilation_config": SimpleNamespace(level=3, cudagraph_mode=_Mode(False)),
        "kv_cache_dtype": "bf16",
        "load_dummy": False,
        "speculative_config": None,
        "hf_config": SimpleNamespace(text_config=SimpleNamespace(**_HF)),
        "max_model_len": 1 << 18,
    }
    fields.update(overrides)
    return SimpleNamespace(**fields)


def _mtp(k):
    return SimpleNamespace(method="mtp", num_speculative_tokens=k)


@pytest.fixture
def native(monkeypatch):
    monkeypatch.setattr(deployment, "is_vllm", lambda: False)
    monkeypatch.setattr(deployment, "is_sglang", lambda: False)
    monkeypatch.setenv("ATOM_MONO_ENABLE", "1")
    monkeypatch.delenv("ATOM_ENABLE_REPLAYSSM", raising=False)
    monkeypatch.delenv("ATOM_REPLAYSSM_CACHE_LEN", raising=False)


@pytest.mark.parametrize("k", [0, 1, 3])
def test_supported_deployment_has_no_refusal(native, k):
    config = _atom_config(speculative_config=_mtp(k) if k else None)
    assert dispatch.config_refusal(config) is None


_CKPT = "/shared_nfs/huggingface_models/Qwen/Qwen3.8-Flash-Next-FP8/config.json"


@pytest.mark.skipif(not os.path.exists(_CKPT), reason="no local checkpoint config")
def test_the_released_checkpoint_has_the_kernel_shapes(native):
    with open(_CKPT) as f:
        text = json.load(f)["text_config"]
    config = _atom_config(
        hf_config=SimpleNamespace(text_config=SimpleNamespace(**text))
    )
    assert dispatch.config_refusal(config) is None


@pytest.mark.parametrize(
    "overrides, reason",
    [
        ({"tensor_parallel_size": 2}, "the kernel serves the whole model (TP1)"),
        ({"enable_expert_parallel": True}, "expert parallel"),
        ({"parallel_config": SimpleNamespace(data_parallel_size=2)}, "DP"),
        ({"pipeline_parallel_size": 2}, "PP"),
        ({"enable_tbo": True}, "TBO"),
        (
            {
                "compilation_config": SimpleNamespace(
                    level=3, cudagraph_mode=_Mode(True)
                )
            },
            "piecewise",
        ),
        ({"kv_cache_dtype": "fp8"}, "kv cache fp8"),
        ({"load_dummy": True}, "dummy weights"),
        (
            {
                "speculative_config": SimpleNamespace(
                    method="eagle3", num_speculative_tokens=3
                )
            },
            "speculative method eagle3",
        ),
        (
            {"speculative_config": _mtp(MAX_TOKENS)},
            f"a verify of {MAX_TOKENS + 1} rows",
        ),
        (
            {"hf_config": SimpleNamespace(**{**_HF, "hc_count": 2})},
            "hc_count 2: the kernel is built for 4",
        ),
        (
            {"hf_config": SimpleNamespace(**{**_HF, "indexer_budget": 4096})},
            "indexer_budget 4096",
        ),
    ],
)
def test_unsupported_deployment_is_refused(native, overrides, reason):
    assert reason in dispatch.config_refusal(_atom_config(**overrides))


def test_switched_off_keeps_the_original_model(native, monkeypatch):
    monkeypatch.setenv("ATOM_MONO_ENABLE", "0")
    model = torch.nn.Linear(1, 1)
    assert dispatch.install_mono_decode(model, _atom_config(), None, None) is model


@pytest.mark.parametrize(
    "k, env, cap", [(0, None, 0), (3, None, 16), (3, "0", 0), (7, "1", 16)]
)
def test_replay_cap_follows_gdn_attn(native, monkeypatch, k, env, cap):
    if env is not None:
        monkeypatch.setenv("ATOM_ENABLE_REPLAYSSM", env)
    config = _atom_config(speculative_config=_mtp(k) if k else None)
    assert dispatch.replay_cap(config) == cap


def _context(monkeypatch, *, prefill=False, draft=False, **gdn):
    g = SimpleNamespace(
        **{
            "num_prefills": 0,
            "num_decodes": 0,
            "spec_sequence_masks": None,
            "spec_state_indices_tensor": None,
            "replayssm": False,
            "slot_idx": None,
            **gdn,
        }
    )
    fwd = SimpleNamespace(
        context=SimpleNamespace(is_prefill=prefill, is_dummy_run=False, is_draft=draft),
        ubatch_slices=None,
        kv_cache_data=True,
        attn_metadata=SimpleNamespace(gdn_metadata=g),
    )
    monkeypatch.setattr(dispatch, "get_forward_context", lambda: fwd)


def _ids(rows):
    return torch.zeros(rows, dtype=torch.int64)


@pytest.mark.parametrize("rows", [1, 3, MAX_TOKENS])
def test_plain_decode_step_is_routed(monkeypatch, rows):
    _context(monkeypatch, num_decodes=rows)
    assert dispatch.step_supported(_ids(rows), 1, False)


@pytest.mark.parametrize(
    "rows, kwargs",
    [(MAX_TOKENS + 1, {}), (2, {"prefill": True}), (2, {"draft": True})],
)
def test_other_plain_steps_keep_the_original_model(monkeypatch, rows, kwargs):
    _context(monkeypatch, **kwargs)
    assert not dispatch.step_supported(_ids(rows), 1, False)


@pytest.mark.parametrize("replay", [True, False])
def test_verify_of_whole_requests_is_routed(monkeypatch, replay):
    slots = torch.zeros(2, 1 if replay else 4, dtype=torch.int32)
    _context(
        monkeypatch,
        spec_sequence_masks=torch.ones(2),
        spec_state_indices_tensor=slots,
        replayssm=replay,
        slot_idx=torch.zeros(2, dtype=torch.int32) if replay else None,
    )
    assert dispatch.step_supported(_ids(8), 4, replay)
