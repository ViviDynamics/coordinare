"""Routing-table models for the self-hosted backend robustness layer (spec 078).

The routing table is the *activation* surface for the layer. It maps a
``(backend, model)`` pair to a :class:`TargetDescriptor` describing how the
self-hosted upstream should be reached:

* ``strategy == "normalize"`` — forward through the loopback shim and apply the
  declared, format-keyed normalizers (e.g. ``harmony_tool_calls``).
* ``strategy == "reroute"`` — repoint the backend's provider env directly at a
  clean upstream and skip the shim entirely (no normalizer).
* ``strategy == "translate"`` — forward through the loopback shim, translating
  the inbound Anthropic ``/v1/messages`` request to an OpenAI
  ``/v1/chat/completions`` request and the OpenAI reply back to Anthropic wire
  (JSON + SSE), optionally composing the declared response normalizers. The
  upstream must be OpenAI-wire (``wire_format == "openai"``); normalizers are
  optional (translation alone is a valid target). (spec 084.)

A ``(backend, model)`` pair with **no entry** resolves to ``None`` — the layer
is then a byte-for-byte no-op (native vendor cloud, FR-078-1/4).

Validation is enforced at config-load time (pydantic), so a malformed target
(e.g. ``reroute`` with normalizers, or an unknown normalizer key) fails fast
rather than black-holing a card mid-lifecycle.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from .normalizers import NORMALIZER_REGISTRY

WireFormat = Literal["openai", "anthropic"]
Strategy = Literal["normalize", "reroute", "translate", "observe"]
# 099: which startup health probe gates this target. ``tool_call`` (default) is
# the existing tool-calling probe; ``completion`` is for non-tool-calling
# backends (e.g. the junie assessor) and gates on a non-empty normalized
# completion instead of a structured tool call.
HealthProbe = Literal["tool_call", "completion"]


def _normalize_backend(backend: str) -> str:
    """Normalize a backend id (kebab → snake, lowercased).

    Mirrors 080's ``test_kebab_case_backend_normalized`` so a routing entry
    written ``claude-code`` resolves a dispatch for ``claude_code``.
    """
    return backend.replace("-", "_").lower()


class TargetDescriptor(BaseModel):
    """A resolved self-hosted upstream a ``(backend, model)`` pair is routed at."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    base_url: str = Field(..., min_length=1)
    wire_format: WireFormat
    strategy: Strategy
    normalizers: list[str] = Field(default_factory=list)
    reroute_upstream: str | None = None
    upstream_model: str | None = None
    # 099: opt-in startup health-probe mode. Default keeps the tool-call probe
    # (byte-for-byte unchanged); ``completion`` gates a non-tool-calling target on
    # a non-empty normalized completion. Invalid value fails at config-load
    # (Literal + extra="forbid").
    health_probe: HealthProbe = "tool_call"
    # 122: opt-in upstream auth injection. Some backend CLIs (junie, pi, hermes)
    # reference the LiteLLM key via an env-var-name/api_key_env indirection their CLI
    # does not resolve onto the wire — so they 401 against an auth-requiring upstream.
    # When set to an env var NAME, the shim overrides the forwarded Authorization
    # with ``Bearer <os.environ[upstream_auth_env]>`` so the coordinare-owned key
    # authenticates regardless of CLI behaviour. (The startup health probe carries
    # its own auth via ``_gate_target``'s probe-token env — OPENAI_API_KEY /
    # ANTHROPIC_AUTH_TOKEN / LITELLM_PROXY_AUTH_TOKEN — so set this to OPENAI_API_KEY
    # to keep probe and traffic on the same key. NB LITELLM_MASTER_KEY is scrubbed
    # from the shim's process env; OPENAI_API_KEY survives.) The secret lives only in
    # the env var; the NAME is all that is configured. Auth-free upstreams (Ollama)
    # ignore the header, so this is safe everywhere.
    upstream_auth_env: str | None = None
    # 150: a per-job shim identity, stable across turns; opt-in for escalation.
    upstream_session_header: Literal["x-switchyard-session-id"] | None = None

    @model_validator(mode="after")
    def _validate_strategy(self) -> TargetDescriptor:
        if self.upstream_session_header and self.reroute_upstream:
            raise ValueError("upstream_session_header cannot use a direct-reroute health fallback")
        if self.strategy == "reroute":
            if self.upstream_session_header is not None:
                raise ValueError("upstream_session_header requires a shim, not direct reroute")
            if self.normalizers:
                raise ValueError(
                    "reroute strategy must declare no normalizers "
                    f"(got {self.normalizers!r}); reroute is not a shim"
                )
            return self

        if self.strategy == "observe":
            # 122 (Decision 6): an observe-passthrough shim forwards the CLI body
            # VERBATIM to base_url and only LOGS latency/status + writes the
            # capture_dir — no response transform. It exists to preserve the
            # coordinare-side request tap once LiteLLM does the repair server-side.
            # It MUST declare no normalizers (it transforms nothing); to repair,
            # use `normalize`.
            if self.normalizers:
                raise ValueError(
                    "observe strategy must declare no normalizers "
                    f"(got {self.normalizers!r}); observe only forwards + logs. "
                    "Use 'normalize' to apply a transform"
                )
            return self

        if self.strategy == "translate":
            # Rule T1 (FR-008): the translator emits OpenAI-wire requests, so an
            # anthropic-wire upstream is contradictory and must be rejected at
            # load rather than black-holing a card mid-lifecycle.
            if self.wire_format != "openai":
                raise ValueError(
                    "translate strategy requires wire_format == 'openai' "
                    f"(got {self.wire_format!r}); the translator converts the "
                    "Anthropic request to OpenAI wire, so an anthropic-wire "
                    "upstream is contradictory"
                )
            # Rule T2: normalizers are OPTIONAL for translate (translation alone
            # is a valid target), but any declared key must be registered.
            unknown = [k for k in self.normalizers if k not in NORMALIZER_REGISTRY]
            if unknown:
                known = sorted(NORMALIZER_REGISTRY)
                raise ValueError(
                    f"unknown normalizer key(s) {unknown!r}; "
                    f"registered normalizers are {known!r}"
                )
            # Rule T3: base_url is already enforced non-empty via Field(min_length=1).
            return self

        # strategy == "normalize"
        if not self.normalizers:
            raise ValueError(
                "normalize strategy requires at least one normalizer key"
            )
        unknown = [k for k in self.normalizers if k not in NORMALIZER_REGISTRY]
        if unknown:
            known = sorted(NORMALIZER_REGISTRY)
            raise ValueError(
                f"unknown normalizer key(s) {unknown!r}; "
                f"registered normalizers are {known!r}"
            )
        return self


class RoutingEntry(BaseModel):
    """One row of the routing table: a ``(backend, model)`` → target mapping."""

    model_config = ConfigDict(extra="forbid")

    backend: str = Field(..., min_length=1)
    model: str = Field(..., min_length=1)
    target: TargetDescriptor

    @model_validator(mode="after")
    def _normalize_backend_id(self) -> RoutingEntry:
        object.__setattr__(self, "backend", _normalize_backend(self.backend))
        return self


class RoutingTable(BaseModel):
    """The config surface mapping ``(backend, model)`` → self-hosted target."""

    model_config = ConfigDict(extra="forbid")

    entries: list[RoutingEntry] = Field(default_factory=list)

    @classmethod
    def from_yaml_file(cls, path: str | Path) -> RoutingTable:
        """Load and validate a routing table from a mounted/baked YAML file.

        The file is the 078 activation surface (see ``SELFHOSTED_ROUTING_CONFIG``):
        a YAML document whose top level is either a mapping keyed by
        ``selfhosted_routing`` (the canonical key used by the contract and
        ``config.example.yaml``) or ``entries``, or a bare list of entries. All
        three shapes are accepted::

            selfhosted_routing:
              - backend: openclaw
                model: gpt-oss-120b
                target: {base_url: "http://ollama:11434", wire_format: openai,
                         strategy: reroute}

        Declaring **both** ``selfhosted_routing`` and ``entries`` is ambiguous
        and fails fast naming both keys.

        Fails fast (``ValueError``) on an unreadable path (missing, a directory,
        permission-denied), a non-mapping/list root, malformed YAML, or a target
        that violates the strategy rules — a broken table must surface at job
        start with an actionable, operator-facing message, not black-hole a card
        mid-lifecycle behind a raw ``OSError`` (FR-078-5).
        """
        p = Path(path)
        try:
            text = p.read_text(encoding="utf-8")
        except OSError as exc:
            # A missing/unreadable mount is the single most common 078
            # misconfiguration. Convert the raw OSError (FileNotFoundError,
            # IsADirectoryError, PermissionError, ...) into a clear message that
            # names the path and the env var that points at it, and says how to
            # recover — instead of surfacing "dispatch failed: FileNotFoundError:
            # [Errno 2] ..." which gives an operator nothing to act on.
            raise ValueError(
                f"routing table {p} (from SELFHOSTED_ROUTING_CONFIG) could not "
                f"be read: {type(exc).__name__}: {exc}. Mount the routing-table "
                f"YAML into the performer container, or unset "
                f"SELFHOSTED_ROUTING_CONFIG to disable self-hosted routing."
            ) from exc
        try:
            data = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            raise ValueError(f"routing table {p} is not valid YAML: {exc}") from exc

        if data is None:
            return cls(entries=[])
        if isinstance(data, list):
            data = {"entries": data}
        if not isinstance(data, dict):
            raise ValueError(
                f"routing table {p} must be a mapping or a list of entries, "
                f"got {type(data).__name__}"
            )
        # The contract and config.example.yaml key the table under
        # ``selfhosted_routing``; the model field is ``entries``. Accept the
        # canonical key by remapping it, but reject a file that declares both
        # (ambiguous — we will not silently drop one).
        if "selfhosted_routing" in data:
            if "entries" in data:
                raise ValueError(
                    f"routing table {p} declares both 'selfhosted_routing' and "
                    f"'entries'; use exactly one (prefer 'selfhosted_routing')"
                )
            data = {
                **{k: v for k, v in data.items() if k != "selfhosted_routing"},
                "entries": data["selfhosted_routing"],
            }
        try:
            return cls.model_validate(data)
        except ValidationError as exc:
            raise ValueError(f"routing table {p} is invalid: {exc}") from exc

    def resolve(self, backend: str, model: str) -> TargetDescriptor | None:
        """Return the target for ``(backend, model)`` or ``None`` if unrouted.

        Backend id is normalized (kebab → snake) on both sides so dispatch and
        config need not agree on punctuation. A missing entry returns ``None``,
        which the launch seam treats as a byte-for-byte no-op (FR-078-1/4).
        """
        wanted_backend = _normalize_backend(backend)
        for entry in self.entries:
            if entry.backend == wanted_backend and entry.model == model:
                return entry.target
        return None
