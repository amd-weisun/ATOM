# SPDX-License-Identifier: MIT
# Copyright (C) 2026, Advanced Micro Devices, Inc. All rights reserved.
"""The Qwen3.8 mono layer kernel: ``layer`` builds the one launch of a decoder layer (Gated DeltaNet or
QSA sparse attention, both inside the hyper-connection mixes, then the MoE) from the stages (``hc``,
``gdn``, ``qsa``, ``out``, ``ffn``) over the Qwen3.5 kernel's helpers."""
