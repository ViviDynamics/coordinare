from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

from coordinare.graph.nodes.monitor.constants import VERDICT_STAGES
from coordinare.lib.redaction import redact_state_value
from coordinare.vocabulary import STAGE_AGENT

RUNNING_PHASES = frozenset({"monitoring_agent", "monitoring_performer"})


def _host(value: object) -> str | None:
    """Expose the host only, never URL credentials, paths or query parameters."""
    if not value:
        return None
    text = str(value)
    try:
        return urlsplit(text if "://" in text else "//" + text).hostname
    except ValueError:
        return None


def _resolution(config: Any, sym: Any, role: str) -> dict[str, Any]:
    empty: dict[str, Any] = {"model": None, "mode": None, "backend": None, "hosts": [], "configured": False}
    if config is None:
        return empty
    try:
        effective = sym.effective_config(config) if sym is not None else config
        performer = effective.performers.resolved_role(role)
        if performer is None:
            return empty
        hosts = sorted({
            host for endpoint in effective.performer_endpoints
            if role in endpoint.roles and (host := _host(endpoint.endpoint))
        })
        direct_host = _host(performer.host or effective.agent_host)
        return {
            "configured": True,
            "model": effective.resolve_performer_dispatch_model(role).get("model"),
            "mode": performer.mode,
            "backend": performer.backend,
            "hosts": hosts or ([direct_host] if direct_host else []),
        }
    except (AttributeError, TypeError, ValueError):
        return empty


def _activity(sessions: dict[str, Any], symphony: str, stage: str) -> list[dict[str, Any]]:
    result = []
    for card_id, session in sessions.items():
        if session.get("performer_stage") != stage or session.get("phase") not in RUNNING_PHASES:
            continue
        dispatch = session.get("agent_dispatch") or {}
        card = session.get("current_card") or {}
        run = dispatch.get("session_id") or session.get("agent_session_id")
        # A dispatch identity is required to call a session running.
        if not run:
            continue
        result.append({
            "symphony": symphony, "card": card_id, "card_title": card.get("title"), "run": run,
        })
    return result


def persona_runtime(state: Any, role: str, scope: frozenset[str]) -> dict[str, Any]:
    """Fresh, allowlisted configuration and live sessions for one stats query's scope."""
    configs = state.get("symphony_configs") or {}
    sym_states = state.get("symphony_states") or {}
    stage = next((stage for stage, agent in STAGE_AGENT.items() if agent == role), None)
    rows = []
    activity = []
    for name in sorted(scope):
        sym = configs.get(name)
        personas = getattr(sym, "personas", None) or {}
        override = personas.get(role) or {}
        text = override.get("instructions", "") if isinstance(override, dict) else ""
        rows.append({
            "symphony": name, "persona_override": isinstance(text, str) and bool(text.strip()),
            **_resolution(state.get("config"), sym, role),
        })
        sessions = dict(getattr(sym_states.get(name), "active_sessions", None) or {})
        # The daemon swaps the current symphony's working sessions into top state.
        # For a single configured symphony this is also the legacy solo path.
        if state.get("current_symphony") == name or len(configs) == 1:
            sessions.update(state.get("active_sessions") or {})
            if not sessions and state.get("current_card"):
                card = state["current_card"]
                card_id = card.get("id") or card.get("node_id")
                if card_id:
                    sessions[card_id] = redact_state_value({
                        key: state.get(key) for key in (
                            "performer_stage", "phase", "agent_dispatch", "agent_session_id", "current_card",
                        )
                    })
        if stage:
            activity.extend(_activity(sessions, name, stage))
    return {"stage": stage, "verdict_applicable": stage in VERDICT_STAGES,
            "symphonies": rows, "activity": activity}
