"""Advocate run settings, delivered through ``workflow_env`` (spec 173).

The labels, templates, keywords and thresholds are coordinare configuration, and
the run needs them. ``workflow_env`` is the existing operator channel into a
workflow and its values are scalars, so lists arrive JSON-encoded.

Every field has a default matching the shipped config, so a run started with an
empty env behaves sensibly rather than refusing to start.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

_DEFAULT_KEYWORDS = [
    "billing", "payment", "legal", "security", "breach",
    "abuse", "harassment", "lawsuit", "GDPR", "refund",
]


def _json_list(env: dict[str, str], key: str, default: list[str]) -> list[str]:
    """A JSON array from *env*, falling back to *default*.

    A malformed value falls back rather than raising: the shipped default for
    every one of these is safe, and refusing to run because an operator typed a
    stray comma would take the role down for a cosmetic error.
    """
    raw = (env.get(key) or "").strip()
    if not raw:
        return list(default)
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return list(default)
    if not isinstance(parsed, list):
        return list(default)
    return [str(x) for x in parsed if str(x).strip()]


def _float(env: dict[str, str], key: str, default: float) -> float:
    try:
        return float(env.get(key) or default)
    except (TypeError, ValueError):
        return default


def _int(env: dict[str, str], key: str, default: int) -> int:
    try:
        return int(env.get(key) or default)
    except (TypeError, ValueError):
        return default


@dataclass
class AdvocateSettings:
    handled_label: str = "advocate-handled"
    escalation_label: str = "needs-human"
    confidence_threshold: float = 0.70
    sensitive_keywords: list[str] = field(default_factory=lambda: list(_DEFAULT_KEYWORDS))
    doc_sources: list[str] = field(default_factory=lambda: ["README.md"])
    support_channel_url: str = ""
    holding_comment_template: str = (
        "Thanks for reaching out. A team member will follow up shortly."
    )
    acknowledgement_template: str = (
        "Thanks for the feature request. We have noted it for our roadmap."
    )
    redirect_template: str = (
        "This does not seem related to the project. "
        "For support, please visit {support_channel_url}."
    )
    disclosure_template: str = (
        "\U0001f916 This response was generated automatically. "
        "Please verify before acting on it."
    )
    #: How many issues one classification call may carry. A bound, not a
    #: preference: a repository with 200 unhandled issues would otherwise build
    #: a prompt no context window holds.
    max_issues_per_call: int = 10
    #: How many issues one run will act on at all.
    max_issues_per_run: int = 30

    @classmethod
    def from_env(cls, env: dict[str, str] | None) -> AdvocateSettings:
        env = env or {}
        defaults = cls()
        return cls(
            handled_label=env.get("ADVOCATE_HANDLED_LABEL") or defaults.handled_label,
            escalation_label=env.get("ADVOCATE_ESCALATION_LABEL") or defaults.escalation_label,
            confidence_threshold=_float(env, "ADVOCATE_CONFIDENCE_THRESHOLD", defaults.confidence_threshold),
            sensitive_keywords=_json_list(env, "ADVOCATE_SENSITIVE_KEYWORDS", defaults.sensitive_keywords),
            doc_sources=_json_list(env, "ADVOCATE_DOC_SOURCES", defaults.doc_sources),
            support_channel_url=env.get("ADVOCATE_SUPPORT_URL") or defaults.support_channel_url,
            holding_comment_template=env.get("ADVOCATE_HOLDING_TEMPLATE") or defaults.holding_comment_template,
            acknowledgement_template=env.get("ADVOCATE_ACK_TEMPLATE") or defaults.acknowledgement_template,
            redirect_template=env.get("ADVOCATE_REDIRECT_TEMPLATE") or defaults.redirect_template,
            disclosure_template=env.get("ADVOCATE_DISCLOSURE_TEMPLATE") or defaults.disclosure_template,
            max_issues_per_call=_int(env, "ADVOCATE_MAX_ISSUES_PER_CALL", defaults.max_issues_per_call),
            max_issues_per_run=_int(env, "ADVOCATE_MAX_ISSUES_PER_RUN", defaults.max_issues_per_run),
        )
