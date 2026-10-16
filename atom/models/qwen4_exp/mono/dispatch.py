# SPDX-License-Identifier: MIT
# Copyright (C) 2026, Advanced Micro Devices, Inc. All rights reserved.
"""Which Qwen3.8-Flash-Next forwards take the mono path.

``install_mono_decode`` wraps the loaded model, the way Qwen3.5's does, and every other attribute passes
through to it. The runner is created at install: it only views ATOM's weights (the FP8 expert bank, the
BF16 matrices, in place), and each launch width's kernels and buffers are built on its first step
(``LazyRunner`` -> ``runner.prepare``).

The configuration is checked once (``config_refusal``). A step is then routed only when it is a pure
decode of up to ``MAX_TOKENS`` rows: a token a request, or an MTP verify of whole requests. The MTP draft
stays on the original modules.
"""

import functools
import logging
from dataclasses import dataclass, fields

from torch import nn

from atom.models.qwen4_exp.mono.config import MAX_TOKENS, Q38Config
from atom.mono.plan.shard import ShardError
from atom.mono.runtime.consensus import MonoUnsupported
from atom.mono.runtime.deployment import common_refusals, first_refusal
from atom.mono.runtime.route import LazyRunner
from atom.utils import envs
from atom.utils.forward_context import get_forward_context

logger = logging.getLogger("atom")

# the TP sizes served (ATOM's recipe of Qwen3.8-Flash-Next)
SUPPORTED_TP = (1,)

# the checkpoint widths the kernel is built for: hf_config field -> Q38Config field
_WIDTHS = {
    "hidden_size": "hidden",
    "hc_count": "hc",
    "hc_lowrank": "hc_lowrank",
    "linear_num_key_heads": "linear_k_heads",
    "linear_num_value_heads": "linear_v_heads",
    "linear_key_head_dim": "linear_k_dim",
    "linear_value_head_dim": "linear_v_dim",
    "linear_conv_kernel_dim": "conv_kernel",
    "num_attention_heads": "heads",
    "num_key_value_heads": "kv_heads",
    "head_dim": "head_dim",
    "indexer_n_heads": "index_heads",
    "indexer_head_dim": "index_dim",
    "indexer_budget": "index_budget",
    "indexer_compress_ratio": "index_ratio",
    "num_experts": "n_experts",
    "num_experts_per_tok": "top_k",
    "moe_intermediate_size": "inter",
    "shared_expert_intermediate_size": "shared_inter",
}


@dataclass(frozen=True)
class Dims:
    """The kernel is built for Qwen3.8-Flash-Next's shapes, served whole (TP1)."""

    hf_config: object
    tp: int

    def __post_init__(self):
        hf, ref = self.hf_config, Q38Config()
        names = {f.name for f in fields(Q38Config)}
        for theirs, ours in _WIDTHS.items():
            assert ours in names, ours
            got = getattr(hf, theirs, None)
            if got != getattr(ref, ours):
                raise ShardError(
                    f"{theirs} {got}: the kernel is built for {getattr(ref, ours)}"
                )
        if self.tp != 1:
            raise ShardError(f"TP {self.tp}: the kernel serves the whole model (TP1)")


def spec_steps(atom_config) -> int:
    """Drafted tokens a request verifies a step at most (0 without a drafter)."""
    spec = atom_config.speculative_config
    return (spec.num_speculative_tokens or 0) if spec is not None else 0


def config_refusal(atom_config) -> str | None:
    """Why this deployment cannot use mono, or None."""
    spec = atom_config.speculative_config
    k = spec_steps(atom_config)
    hf = getattr(atom_config.hf_config, "text_config", atom_config.hf_config)
    dims = functools.partial(Dims, hf)
    checks = common_refusals(atom_config, dims, SUPPORTED_TP) + [
        (
            atom_config.kv_cache_dtype == "bf16",
            f"kv cache {atom_config.kv_cache_dtype}",
        ),
        (not atom_config.load_dummy, "dummy weights"),
        (
            spec is None or spec.method == "mtp",
            f"speculative method {spec.method if spec else None}",
        ),
        # a verify launch holds whole requests: one at least
        (k + 1 <= MAX_TOKENS, f"a verify of {k + 1} rows > {MAX_TOKENS}"),
    ]
    return first_refusal(checks)


def replay_cap(atom_config) -> int:
    """ReplaySSM's record buffer length, or 0 when ATOM keeps the snapshot GDN state: gdn_attn's rule
    (on by default under speculative decoding, ``ATOM_ENABLE_REPLAYSSM`` overriding either way; the
    length raised to two verify windows)."""
    k = spec_steps(atom_config)
    on = k > 0 if envs.ATOM_ENABLE_REPLAYSSM is None else envs.ATOM_ENABLE_REPLAYSSM
    return max(envs.ATOM_REPLAYSSM_CACHE_LEN, 2 * (k + 1)) if on and k > 0 else 0


def step_supported(input_ids, T: int, replay: bool) -> bool:
    """Is this forward a pure decode of whole requests, up to ``MAX_TOKENS`` rows?"""
    rows = input_ids.numel()
    if not 0 < rows <= MAX_TOKENS or rows % T:
        return False
    fwd = get_forward_context()
    c = fwd.context
    if c is None or c.is_prefill or c.is_dummy_run or getattr(c, "is_draft", False):
        return False
    if getattr(fwd, "ubatch_slices", None) is not None:
        return False
    g = getattr(fwd.attn_metadata, "gdn_metadata", None)
    if g is None or g.num_prefills != 0:
        return False
    if T == 1:
        return g.spec_sequence_masks is None
    # an MTP verify: every request a spec decode, on the GDN state the runner was built
    # for (ReplaySSM: a checkpoint slot a request; snapshot: T slots a request)
    if (
        g.spec_sequence_masks is None
        or g.num_decodes != 0
        or g.spec_state_indices_tensor is None
        or bool(getattr(g, "replayssm", False)) != replay
    ):
        return False
    return (
        g.slot_idx is not None if replay else g.spec_state_indices_tensor.shape[1] == T
    )


class MonoDecodeModel(nn.Module):
    """The loaded Qwen3.8-Flash-Next model, with its supported decode steps routed to mono."""

    def __init__(self, model: nn.Module, runner) -> None:
        super().__init__()
        self.model = model
        self._runner = runner
        # the runner exists already; each width is prepared on its first step
        self._mono = LazyRunner(lambda: runner, "Qwen3.8 mono decode")

    def forward(
        self, input_ids, positions, intermediate_tensors=None, inputs_embeds=None, **kw
    ):
        if (
            inputs_embeds is None
            and intermediate_tensors is None
            and not kw
            # the engine's warmup runs before the KV cache is bound
            and get_forward_context().kv_cache_data
            and step_supported(input_ids, self._runner.T, self._runner.replay_cap > 0)
            and self._mono.ready(input_ids.numel())
        ):
            return self._mono.runner.forward(input_ids, positions)
        return self.model(
            input_ids, positions, intermediate_tensors, inputs_embeds, **kw
        )

    def compute_logits(self, hidden_states):
        return self.model.compute_logits(hidden_states)

    def __getattr__(self, name: str):
        try:
            return super().__getattr__(name)
        except AttributeError:
            return getattr(self.model, name)


def install_mono_decode(
    model: nn.Module, atom_config, drafter, metadata_builder
) -> nn.Module:
    """``model`` wrapped for mono decode, or ``model`` itself when this deployment is one mono does
    not serve."""
    del metadata_builder
    if not envs.ATOM_MONO_ENABLE:
        return model
    why = config_refusal(atom_config)
    if why is not None:
        logger.info("Qwen3.8 mono decode off: %s", why)
        return model
    # the kernels (FlyDSL) load only once the deployment is one mono serves
    try:
        from atom.models.qwen4_exp.mono.runner import Qwen38MonoDecodeRunner
    except ImportError as e:
        logger.warning("Qwen3.8 mono decode off: %s", e)
        return model
    try:
        steps = int(drafter.mtp_k) if drafter is not None else 0
        runner = Qwen38MonoDecodeRunner(
            model, atom_config, steps, replay_cap(atom_config)
        )
    except MonoUnsupported as why:
        logger.warning("Qwen3.8 mono decode off: %s", why)
        return model
    return MonoDecodeModel(model, runner)
