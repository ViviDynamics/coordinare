"""Which stages and symphonies use each configured item (#143).

The Settings page's "Used by" lists come from this module and nowhere else.
The references follow the config's own links:

* a performer role names a mode; the mode names up to three models (tool,
  thinking, classifier); each model names one model server;
* a role runs in its stage, and in every symphony that runs the role;
* a persona is used wherever its role runs;
* a routing entry is used by the roles whose backend is the entry's backend
  and whose mode uses a model with the entry's model name.

A symphony counts as running a role when the global performer config
configures it, unless the symphony overrides that role's backend or mode.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel

from coordinare.lifecycle import ROLE_TO_STAGE

if TYPE_CHECKING:
    from coordinare.config import CoordinareConfiguration, PerformersConfig

UsageKind = Literal["stage", "symphony", "mode", "model", "model_server", "persona"]

#: The three model references a mode can make (config.Mode).
MODEL_FIELDS: tuple[str, ...] = ("tool", "thinking", "classifier")


class UsedBy(BaseModel):
    """One item that uses another: a stage, a symphony, or a catalog entry."""

    kind: UsageKind
    name: str


@dataclass(frozen=True)
class _Edge:
    """One role as it runs in one place: globally (symphony None) or in one symphony."""

    role: str
    backend: str | None
    mode: str | None
    symphony: str | None


def _performer_roles(performers: PerformersConfig) -> list[str]:
    return [name for name in type(performers).model_fields if name != "default"]


def _global_role_field(performers: PerformersConfig, role: str, field: str) -> str | None:
    """A role's value for ``field``, falling back to the ``performers.default`` entry."""
    role_cfg = getattr(performers, role, None)
    value = getattr(role_cfg, field, None) if role_cfg is not None else None
    if value is None and performers.default is not None:
        value = getattr(performers.default, field, None)
    return value


def _symphony_role_override(symphony_overrides: dict[str, Any] | None, role: str) -> dict[str, Any]:
    """The per-symphony override block for one role, or an empty dict."""
    performers = (symphony_overrides or {}).get("performers")
    if not isinstance(performers, dict):
        return {}
    entry = performers.get(role)
    return entry if isinstance(entry, dict) else {}


def _role_edges(config: CoordinareConfiguration) -> list[_Edge]:
    performers = config.global_config.performers
    roles = [role for role in _performer_roles(performers) if getattr(performers, role) is not None]
    edges: list[_Edge] = []
    for role in roles:
        edges.append(_Edge(
            role,
            _global_role_field(performers, role, "backend"),
            _global_role_field(performers, role, "mode"),
            None,
        ))
    for symphony in config.symphonies:
        for role in roles:
            override = _symphony_role_override(symphony.overrides, role)
            backend = override.get("backend") or _global_role_field(performers, role, "backend")
            mode = override.get("mode") or _global_role_field(performers, role, "mode")
            edges.append(_Edge(role, backend, mode, symphony.name))
    return edges


def _edge_users(edge: _Edge) -> list[UsedBy]:
    users = [UsedBy(kind="stage", name=ROLE_TO_STAGE.get(edge.role, edge.role))]
    if edge.symphony is not None:
        users.append(UsedBy(kind="symphony", name=edge.symphony))
    return users


def _dedupe(items: list[UsedBy]) -> list[UsedBy]:
    seen: set[tuple[str, str]] = set()
    unique: list[UsedBy] = []
    for item in items:
        key = (item.kind, item.name)
        if key not in seen:
            seen.add(key)
            unique.append(item)
    return unique


def usage_index(config: CoordinareConfiguration) -> dict[tuple[str, str], list[UsedBy]]:
    """Every catalog item and persona, keyed by ``(kind, name)``, with its users.

    ``kind`` is one of ``mode``, ``model``, ``model_server`` or ``persona``. An
    item nothing uses maps to an empty list, which the page shows as "Not used".
    A persona whose role is not configured has no entry, so read it with ``.get``.
    """
    pc = config.global_config
    edges = _role_edges(config)

    mode_users: dict[str, list[UsedBy]] = {}
    persona_users: dict[str, list[UsedBy]] = {}
    for edge in edges:
        persona_users.setdefault(edge.role, []).extend(_edge_users(edge))
        if edge.mode is not None:
            mode_users.setdefault(edge.mode, []).extend(_edge_users(edge))

    index: dict[tuple[str, str], list[UsedBy]] = {}
    for role, users in persona_users.items():
        index[("persona", role)] = _dedupe(users)

    # A mode's users are its roles' stages and symphonies. A model is used by
    # the modes that name it, and so by their users. A server is used by the
    # models it serves, and so by their users too.
    model_users: dict[str, list[UsedBy]] = {model.name: [] for model in pc.model_endpoints}
    for mode in pc.modes:
        users = _dedupe(mode_users.get(mode.name, []))
        index[("mode", mode.name)] = users
        for field in MODEL_FIELDS:
            ref = getattr(mode, field, None)
            if ref in model_users:
                model_users[ref].extend([UsedBy(kind="mode", name=mode.name), *users])
    for model_name, users in model_users.items():
        index[("model", model_name)] = _dedupe(users)

    server_users: dict[str, list[UsedBy]] = {endpoint.name: [] for endpoint in pc.endpoints}
    for model in pc.model_endpoints:
        if model.endpoint in server_users:
            server_users[model.endpoint].extend(
                [UsedBy(kind="model", name=model.name), *index.get(("model", model.name), [])],
            )
    for server_name, users in server_users.items():
        index[("model_server", server_name)] = _dedupe(users)
    return index


def routing_used_by(config: CoordinareConfiguration, backend: str, model: str) -> list[UsedBy]:
    """The stages and symphonies a routing entry (backend, model) applies to.

    The entry applies when a role runs on ``backend`` and its mode names a model
    whose ``model`` string is ``model``.
    """
    pc = config.global_config
    matching_models = {entry.name for entry in pc.model_endpoints if entry.model == model}
    modes_with_model = {
        mode.name for mode in pc.modes
        if any(getattr(mode, field, None) in matching_models for field in MODEL_FIELDS)
    }
    users: list[UsedBy] = []
    for edge in _role_edges(config):
        if edge.backend == backend and edge.mode in modes_with_model:
            users.extend(_edge_users(edge))
    return _dedupe(users)
