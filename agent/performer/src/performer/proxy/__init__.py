"""080 — dual-model planner/executor proxy.

An in-container reverse proxy that pairs a thinking model with a tool-calling
model within a single agent turn. Generalizes the 073 ``ClaudeCodeShim`` seam
(CLI → model-provider) into a configurable orchestration proxy. Activated only
when a performer's mode strategy is not ``single``.

Units:
- ``llm_turn``    — format-neutral canonical representation (LLMRequest/LLMResponse)
- ``upstreams``   — per-format clients (anthropic / openai) over httpx
- ``strategies``  — OrchestrationStrategy protocol + always/conditional/think_once
- ``assembler``   — merge plan + tool output into one wire-correct response
- ``classifier``  — difficulty pre-pass for conditional escalation
- ``dual_model_proxy`` — the aiohttp transport shell
"""

from __future__ import annotations
