"""Config descriptor layer for the dashboard live-config UI (spec 081-config-ui).

This module introspects the existing pydantic config models
(:class:`coordinare.config.ProjectConfiguration`,
:class:`coordinare.config.CoordinareConfiguration`, and the spec-080 catalogs)
and projects them into UI-renderable *descriptors* — read-only view/transfer
objects the dashboard renders and the save endpoints validate against.

Design references (specs/081-config-ui/):
  * research D1 — derive descriptors by introspecting pydantic ``model_fields`` /
    ``model_json_schema()`` plus a per-field annotation table for the flags
    (``editable`` / ``restart_required`` / ``secret``) that cannot be inferred
    from the type system.
  * research D3 — secret masking and ``${VAR}`` literal preservation, applied at
    a single choke-point serializer.
  * research D8 — invalid-on-disk fields render best-effort, read-only, with a
    marker; the snapshot never raises.
  * data-model E1/E2/E3/E5 — ConfigSetting / ConfigSection / CatalogItem /
    ConfigSnapshot shapes.

The descriptor layer is **read-only**: all validation happens on the write side
(``config_write_service`` / ``routing_config_service``) against the pydantic
models. Nothing here mutates config or expands ``${VAR}`` placeholders.
"""

from __future__ import annotations

import enum
import re
import types
import typing
from pathlib import PurePath
from typing import TYPE_CHECKING, Any, Literal, Union

from pydantic import BaseModel, Field
from pydantic_core import PydanticUndefined

if TYPE_CHECKING:
    from pydantic.fields import FieldInfo

    from coordinare.config import (
        CoordinareConfiguration,
        Endpoint,
        Mode,
        ModelEndpoint,
        ProjectConfiguration,
    )

# --- transfer objects (data-model E1/E2/E3/E5) ---------------------------------

SettingType = Literal["string", "int", "float", "bool", "enum", "list", "text", "secret"]
Store = Literal["config_yaml", "routing_yaml"]
SectionKind = Literal["scalar_group", "collection"]

SECRET_MASK = "••••••"  # display mask, not a credential


class ConfigSetting(BaseModel):
    """Atomic UI-renderable descriptor for a single config field (data-model E1)."""

    key: str
    label: str
    help: str | None = None
    type: SettingType
    current_value: Any | None = None
    default: Any | None = None
    range: dict[str, Any] | None = None
    enum: list[str] | None = None
    editable: bool = True
    restart_required: bool = False
    secret: bool = False
    is_env_placeholder: bool = False
    invalid: bool = False
    section: str = ""
    store: Store = "config_yaml"


class CatalogItem(BaseModel):
    """One entity in a ``collection`` section (data-model E3)."""

    id: str
    kind: str
    settings: list[ConfigSetting] = Field(default_factory=list)
    referenced_by: list[str] = Field(default_factory=list)
    deletable: bool = True


class ConfigSection(BaseModel):
    """A logical grouping of settings for the UI (data-model E2)."""

    id: str
    title: str
    description: str | None = None
    store: Store = "config_yaml"
    kind: SectionKind = "scalar_group"
    settings: list[ConfigSetting] = Field(default_factory=list)
    items: list[CatalogItem] = Field(default_factory=list)
    invalid_banner: str | None = None


class ConfigSnapshot(BaseModel):
    """The full editable surface returned by ``GET /api/config/all`` (data-model E5)."""

    sections: list[ConfigSection] = Field(default_factory=list)
    content_hashes: dict[str, str | None] = Field(default_factory=dict)
    config_version: int = 0
    routing_available: bool = False


# --- T007: secret masking + ${VAR} preservation (research D3) ------------------


_ENV_PLACEHOLDER_RE = re.compile(r"\$\{[A-Za-z_][A-Za-z0-9_]*\}")


def is_env_placeholder(value: Any) -> bool:
    """Return ``True`` if ``value`` is a string containing a ``${VARNAME}`` placeholder.

    Matches a contiguous ``${VARNAME}`` token (a leading letter/underscore then
    word chars), allowing prefixes/suffixes like ``${FOO}/bar``. A mere bare
    presence of ``${`` and ``}`` does NOT count — a secret literal that happens to
    contain those characters (e.g. ``"}text ${"`` or ``"${ has space }"``) must
    still be masked, never passed through verbatim (spec 081: never expose
    secrets).

    ``${VARNAME}`` literals are written back to disk verbatim — they are NEVER
    expanded (expansion happens only at load via ``os.path.expandvars``). The UI
    shows them as-is rather than masking, so an operator can see and edit which
    env var supplies the value.
    """
    return isinstance(value, str) and _ENV_PLACEHOLDER_RE.search(value) is not None


def serialize_value(raw: Any, *, secret: bool) -> tuple[Any, bool]:
    """Project a raw config value into ``(display_value, is_env_placeholder)``.

    The single choke-point for secret masking (research D3):
      * ``${VAR}`` literals pass through verbatim (never masked, never expanded).
      * a non-placeholder secret with a real value masks to :data:`SECRET_MASK`.
      * ``None`` is returned unchanged (an unset secret has nothing to hide —
        masking it would falsely imply a value exists).
      * everything else passes through.
    """
    is_env = is_env_placeholder(raw)
    if is_env:
        return raw, True
    if secret and raw is not None:
        return SECRET_MASK, False
    return raw, False


# --- T005: per-field annotation table (research D1) ----------------------------


class FieldAnnotation(BaseModel):
    """Flags for a config field that cannot be inferred from the type system.

    Keyed by dotted ``<section>.<field>`` path in :data:`_ANNOTATIONS`. Supplies
    ``editable`` / ``restart_required`` / ``secret`` plus optional ``range`` /
    ``enum`` overrides for fields whose constraints live in a validator rather
    than in the pydantic ``Field`` metadata.
    """

    editable: bool = True
    restart_required: bool = False
    secret: bool = False
    range: dict[str, Any] | None = None
    enum: list[str] | None = None


# Restart-required fields are process bindings (ports/hosts) read once at startup;
# everything else is hot-reloadable. Secrets are tokens/keys held in config — note
# ``auth_env`` is the NAME of an env var, never the secret, so it is NOT flagged.
_DEFAULT_ANNOTATION = FieldAnnotation()

_ANNOTATIONS: dict[str, FieldAnnotation] = {
    # --- secrets ---
    "global.github_token": FieldAnnotation(secret=True),
    "global.dashboard_auth_token": FieldAnnotation(secret=True, restart_required=True),
    # --- restart-required process bindings ---
    "global.dashboard_port": FieldAnnotation(restart_required=True),
    "global.dashboard_host": FieldAnnotation(restart_required=True),
    "global.health_check_port": FieldAnnotation(restart_required=True),
    # --- range overrides (constraint enforced in a validator, not Field()) ---
    "modes.threshold": FieldAnnotation(range={"min": 0.0, "max": 1.0}),
}


def annotation_for(key: str) -> FieldAnnotation:
    """Return the :class:`FieldAnnotation` for a dotted ``<section>.<field>`` key.

    Falls back to a permissive default (editable, no restart, not secret) for any
    key not in the table — an unknown field is treated as a plain editable scalar.
    """
    return _ANNOTATIONS.get(key, _DEFAULT_ANNOTATION)


# --- T006: pydantic-field introspection into ConfigSetting (research D1) -------


def _unwrap_optional(annotation: Any) -> Any:
    """Strip ``| None`` from an annotation, returning the inner type."""
    origin = typing.get_origin(annotation)
    if origin is Union or origin is types.UnionType:
        non_none = [a for a in typing.get_args(annotation) if a is not type(None)]
        if len(non_none) == 1:
            return non_none[0]
    return annotation


def _infer_enum(annotation: Any) -> list[str] | None:
    """Return the choices for a ``Literal[...]`` or an ``enum.Enum`` subclass.

    Both surface as a ``type="enum"`` dropdown in the UI. ``StrEnum``/``Enum``
    fields (e.g. ``ProjectConfiguration.branch_collision_strategy``) expose their
    member values; ``Literal`` exposes its argument values. Returns ``None`` for
    any other annotation.
    """
    if typing.get_origin(annotation) is Literal:
        return [str(a) for a in typing.get_args(annotation)]
    if isinstance(annotation, type) and issubclass(annotation, enum.Enum):
        return [str(member.value) for member in annotation]
    return None


def _infer_type(annotation: Any) -> SettingType:
    """Map an unwrapped python annotation to a UI :data:`SettingType`."""
    if _infer_enum(annotation) is not None:
        return "enum"
    # bool must precede int — ``bool`` is a subclass of ``int``.
    if annotation is bool:
        return "bool"
    if annotation is int:
        return "int"
    if annotation is float:
        return "float"
    origin = typing.get_origin(annotation)
    if annotation in (list, tuple, set) or origin in (list, tuple, set):
        return "list"
    return "string"


def _infer_range(field_info: FieldInfo) -> dict[str, Any] | None:
    """Extract a range from pydantic ``annotated_types`` metadata.

    Numeric bounds (Ge/Gt/Le/Lt) surface as ``{min, max}``; string/collection
    length bounds (MinLen/MaxLen, i.e. ``Field(min_length=.../max_length=...)``)
    surface as ``{min_length, max_length}`` so the UI can show the allowed range
    for string fields too.

    ``gt``/``lt`` are *exclusive* bounds — collapsing them into a bare ``min``/``max``
    would let the descriptor imply that the boundary value itself is allowed (e.g.
    ``Field(gt=0.0)`` would read as "min 0.0", but ``0.0`` is rejected by server
    validation). Exclusive bounds therefore carry a ``min_exclusive`` / ``max_exclusive``
    flag so the UI can render ``>``/``<`` instead of ``≥``/``≤``.
    """
    out: dict[str, Any] = {}
    for meta in field_info.metadata:
        # annotated_types carries each bound on a single named attribute. The last
        # three entries set the exclusivity flag for the bound they accompany.
        for attr, key, exclusive_key in (
            ("ge", "min", None),
            ("gt", "min", "min_exclusive"),
            ("le", "max", None),
            ("lt", "max", "max_exclusive"),
            ("min_length", "min_length", None),
            ("max_length", "max_length", None),
        ):
            bound = getattr(meta, attr, None)
            if bound is not None:
                out[key] = bound
                if exclusive_key is not None:
                    out[exclusive_key] = True
    return out or None


def _json_safe(value: Any) -> Any:
    """Coerce a config value into a JSON-renderable scalar/structure.

    Unwraps ``SecretStr`` (so the choke-point serializer can mask it), ``Path``,
    ``Enum``, and nested pydantic models — never expanding ``${VAR}`` literals
    (those are plain strings and pass straight through).
    """
    if hasattr(value, "get_secret_value"):
        return value.get_secret_value()
    if isinstance(value, PurePath):
        return str(value)
    if isinstance(value, enum.Enum):
        return value.value
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    return value


def setting_from_field(
    section: str,
    key: str,
    field_info: FieldInfo,
    value: Any,
    *,
    invalid: bool = False,
) -> ConfigSetting:
    """Project one pydantic field + its current value into a :class:`ConfigSetting`.

    Infers type / enum / range from the field annotation and metadata, applies the
    per-field annotation flags (:func:`annotation_for`), and masks secrets via the
    choke-point serializer (:func:`serialize_value`). The dotted ``<section>.<key>``
    becomes the setting ``key``. An ``invalid`` field (failed validation on disk,
    research D8) renders read-only with its raw value shown.
    """
    dotted = f"{section}.{key}"
    ann = annotation_for(dotted)

    inner = _unwrap_optional(field_info.annotation)
    enum_choices = ann.enum if ann.enum is not None else _infer_enum(inner)
    inferred_type = _infer_type(inner)
    setting_type: SettingType = "secret" if ann.secret else inferred_type
    rng = ann.range if ann.range is not None else _infer_range(field_info)

    display, is_env = serialize_value(_json_safe(value), secret=ann.secret)

    default = field_info.default
    if default is PydanticUndefined:
        # Fields declared with default_factory leave FieldInfo.default unset;
        # call the factory so the UI shows the real default instead of None.
        if field_info.default_factory is not None:
            try:
                default = field_info.default_factory()  # type: ignore[call-arg]
            except TypeError:
                # Pydantic supports validated-data-aware factories (one arg);
                # we have no validated data here, so fall back to no default.
                default = None
        else:
            default = None
    default = _json_safe(default)

    return ConfigSetting(
        key=dotted,
        label=key,
        help=field_info.description,
        type=setting_type,
        current_value=display,
        default=default,
        range=rng,
        enum=enum_choices,
        editable=ann.editable and not invalid,
        restart_required=ann.restart_required,
        secret=ann.secret,
        is_env_placeholder=is_env,
        invalid=invalid,
        section=section,
    )


# --- T013/T014: section grouping + snapshot assembly (data-model E2/E3/E5) -----

# The performer roles that can carry a ``mode`` reference (see
# ProjectConfiguration._validate_orchestration_catalogs).
_PERFORMER_ROLE_NAMES: tuple[str, ...] = (
    "default", "advocate", "curator", "assessor", "architect", "implementer",
    "reviewer", "security", "qa", "tech_writer", "closer", "env_bootstrap",
)


def _is_scalar_field(field_info: FieldInfo) -> bool:
    """True when a field is a renderable scalar (not a nested model / catalog).

    Nested ``BaseModel`` fields and lists-of-``BaseModel`` (the spec-080 catalogs,
    personas, performers, etc.) live in their own sections, never the global
    scalar group. Scalar lists (``list[str]``) stay.
    """
    inner = _unwrap_optional(field_info.annotation)
    if isinstance(inner, type) and issubclass(inner, BaseModel):
        return False
    if typing.get_origin(inner) in (list, tuple, set):
        args = typing.get_args(inner)
        if args and isinstance(args[0], type) and issubclass(args[0], BaseModel):
            return False
    return True


#: What ``PUT /api/config/global`` will actually accept. Narrower than the section's
#: scalar fields on purpose -- most of them are not safely editable while running.
#:
#: 155 (#202): lifted here from a closure inside ``create_dashboard_app`` so the config
#: assistant validates proposals against the same set the endpoint enforces. When those
#: two disagreed, a proposal could pass validation and then be refused at Apply, making
#: the button the place errors surface.
GLOBAL_EDITABLE_FIELDS: tuple[str, ...] = (
    "poll_interval_seconds",
    "heartbeat_interval_seconds",
    "max_concurrent_cards",
    "max_feedback_cycles",
    "max_closed_pr_attempts_per_issue",
    "log_level",
    "output_mode",
    "assignee_filter",
    "human_reviewers",
    "trusted_bot_reviewers",
    "env_cache_root",
)


def scalar_field_names(section: str) -> frozenset[str]:
    """Return the editable scalar field names belonging to a scalar-group section.

    The only scalar group is ``global`` → :class:`ProjectConfiguration`'s scalar
    fields (``_is_scalar_field`` excludes nested models and the spec-080 catalogs,
    which have their own CRUD endpoints). Used by ``save_section`` to reject any key
    that is not a real scalar field of the section — without this guard, because
    ``ProjectConfiguration`` is ``extra="ignore"``, unknown or non-scalar keys would
    be silently persisted into ``config.yaml``. Returns an empty set for any
    non-scalar / unknown section.
    """
    from coordinare.config import ProjectConfiguration

    if section != "global":
        return frozenset()
    return frozenset(
        fname
        for fname, finfo in ProjectConfiguration.model_fields.items()
        if _is_scalar_field(finfo)
    )


def _invalid_banner(invalid: dict[str, str] | None, section_id: str) -> str | None:
    """Build a section banner naming the invalid-on-disk fields (research D8)."""
    if not invalid:
        return None
    prefix = f"{section_id}."
    bad = sorted(k for k in invalid if k.startswith(prefix))
    if not bad:
        return None
    fields = ", ".join(k[len(prefix):] for k in bad)
    return (
        "Some fields could not be loaded from config.yaml and are shown "
        f"read-only until the on-disk value is fixed: {fields}."
    )


def _build_global_section(
    config: CoordinareConfiguration,
    *,
    invalid: dict[str, str] | None,
    raw_values: dict[str, Any] | None,
) -> ConfigSection:
    pc: ProjectConfiguration = config.global_config
    settings: list[ConfigSetting] = []
    for fname, finfo in type(pc).model_fields.items():
        if not _is_scalar_field(finfo):
            continue
        dotted = f"global.{fname}"
        is_invalid = dotted in (invalid or {})
        if raw_values is not None and dotted in raw_values:
            value = raw_values[dotted]
        else:
            value = getattr(pc, fname, None)
        settings.append(
            setting_from_field("global", fname, finfo, value, invalid=is_invalid),
        )
    return ConfigSection(
        id="global",
        title="Global",
        description="Coordinare-wide tuning applied to every symphony.",
        kind="scalar_group",
        settings=settings,
        invalid_banner=_invalid_banner(invalid, "global"),
    )


def _catalog_settings(section_id: str, item: BaseModel) -> list[ConfigSetting]:
    return [
        setting_from_field(section_id, fname, finfo, getattr(item, fname))
        for fname, finfo in type(item).model_fields.items()
    ]


def _build_personas_section(config: CoordinareConfiguration) -> ConfigSection:
    personas = config.global_config.personas
    items: list[CatalogItem] = []
    for role_name in type(personas).model_fields:
        persona = getattr(personas, role_name)
        items.append(
            CatalogItem(
                id=role_name,
                kind="persona",
                settings=_catalog_settings("personas", persona),
                referenced_by=[],
                deletable=True,
            ),
        )
    return ConfigSection(
        id="personas",
        title="Personas",
        description="Per-role behavioral instructions.",
        kind="collection",
        items=items,
    )


def _build_symphonies_section(config: CoordinareConfiguration) -> ConfigSection:
    items = [
        CatalogItem(
            id=sym.name,
            kind="symphony",
            settings=_catalog_settings("symphonies", sym),
            referenced_by=[],
            deletable=True,
        )
        for sym in config.symphonies
    ]
    return ConfigSection(
        id="symphonies",
        title="Symphonies",
        description="Project orchestrations bound to GitHub boards.",
        kind="collection",
        items=items,
    )


def _build_catalog_section(
    section_id: str,
    title: str,
    description: str,
    items: list[Endpoint | ModelEndpoint | Mode],
    ref_map: dict[str, list[str]],
) -> ConfigSection:
    catalog_items = [
        CatalogItem(
            id=item.name,
            kind=section_id,
            settings=_catalog_settings(section_id, item),
            referenced_by=ref_map.get(item.name, []),
            deletable=not ref_map.get(item.name),
        )
        for item in items
    ]
    return ConfigSection(
        id=section_id,
        title=title,
        description=description,
        kind="collection",
        items=catalog_items,
    )


def _build_catalog_sections(config: CoordinareConfiguration) -> list[ConfigSection]:
    """endpoints / model_endpoints / modes with referential-integrity delete-protection.

    Reference DAG (research D6): model_endpoint.endpoint → endpoint;
    mode.{tool,thinking,classifier} → model_endpoint; performer.mode → mode.
    An item referenced by anything is not deletable; ``referenced_by`` names the
    referrers.
    """
    pc = config.global_config

    endpoint_refs: dict[str, list[str]] = {e.name: [] for e in pc.endpoints}
    for me in pc.model_endpoints:
        if me.endpoint in endpoint_refs:
            endpoint_refs[me.endpoint].append(me.name)

    model_endpoint_refs: dict[str, list[str]] = {m.name: [] for m in pc.model_endpoints}
    for mode in pc.modes:
        for field in ("tool", "thinking", "classifier"):
            ref = getattr(mode, field, None)
            if ref in model_endpoint_refs and mode.name not in model_endpoint_refs[ref]:
                model_endpoint_refs[ref].append(mode.name)

    mode_refs: dict[str, list[str]] = {m.name: [] for m in pc.modes}
    for role_name in _PERFORMER_ROLE_NAMES:
        role = getattr(pc.performers, role_name, None)
        role_mode = getattr(role, "mode", None) if role is not None else None
        if role_mode in mode_refs:
            mode_refs[role_mode].append(role_name)

    return [
        _build_catalog_section(
            "endpoints",
            "Endpoints",
            "Model-serving locations (spec-080).",
            list(pc.endpoints),
            endpoint_refs,
        ),
        _build_catalog_section(
            "model_endpoints",
            "Model Endpoints",
            "Named (model @ endpoint) pairs (spec-080).",
            list(pc.model_endpoints),
            model_endpoint_refs,
        ),
        _build_catalog_section(
            "modes",
            "Modes",
            "Named orchestration behaviors (spec-080).",
            list(pc.modes),
            mode_refs,
        ),
    ]


def _build_routing_section(
    *, routing_available: bool, routing_mounted: bool = False,
) -> ConfigSection:
    """Routing-table section (spec-078). Empty read-only state when unavailable.

    The editable routing items are populated in Phase 5; for now an available
    routing config still renders an (empty) collection, and an unavailable one
    renders the empty-state banner (research D8 / contract config-api.md).

    ``routing_available`` is false in two distinct situations and the banner must
    not conflate them: (a) no performer endpoint mounts a routing path at all, vs
    (b) an endpoint *does* mount one but the host-side file is missing / not a
    regular file. ``routing_mounted`` distinguishes them so a broken mount is not
    misdiagnosed as "unmounted".
    """
    banner = None
    if not routing_available:
        if routing_mounted:
            banner = (
                "A routing table is mounted by a performer endpoint, but its host "
                "file is missing or not a regular file, so routing is not editable "
                "here. "
                "Check the endpoint volume mount + SELFHOSTED_ROUTING_CONFIG path."
            )
        else:
            banner = (
                "No performer routing table is mounted on this host, so routing is "
                "not editable here. Mount a routing YAML via a performer endpoint "
                "volume + SELFHOSTED_ROUTING_CONFIG to manage it."
            )
    return ConfigSection(
        id="routing",
        title="Routing",
        description="Self-hosted backend routing table (spec-078).",
        store="routing_yaml",
        kind="collection",
        items=[],
        invalid_banner=banner,
    )


def build_snapshot(
    config: CoordinareConfiguration,
    *,
    config_version: int = 0,
    routing_available: bool = False,
    routing_mounted: bool = False,
    content_hashes: dict[str, str | None] | None = None,
    invalid: dict[str, str] | None = None,
    raw_values: dict[str, Any] | None = None,
) -> ConfigSnapshot:
    """Build the full :class:`ConfigSnapshot` for ``GET /api/config/all`` (T013).

    Assembles all 7 sections (global scalar group + personas / symphonies /
    endpoints / model_endpoints / modes / routing collections), masks secrets,
    preserves ``${VAR}`` literals (display values sourced from ``raw_values`` —
    the raw on-disk YAML, research D3), and renders invalid-on-disk fields
    (``invalid``) read-only (research D8). Never raises.
    """
    sections = [
        _build_global_section(config, invalid=invalid, raw_values=raw_values),
        _build_personas_section(config),
        _build_symphonies_section(config),
        *_build_catalog_sections(config),
        _build_routing_section(
            routing_available=routing_available, routing_mounted=routing_mounted,
        ),
    ]
    return ConfigSnapshot(
        sections=sections,
        content_hashes=content_hashes or {},
        config_version=config_version,
        routing_available=routing_available,
    )


def build_section(
    config: CoordinareConfiguration,
    section_id: str,
    *,
    routing_available: bool = False,
    routing_mounted: bool = False,
    content_hashes: dict[str, str | None] | None = None,
    invalid: dict[str, str] | None = None,
    raw_values: dict[str, Any] | None = None,
) -> ConfigSection:
    """Build a single :class:`ConfigSection` for ``GET /api/config/section/{id}`` (T017).

    Raises :class:`KeyError` for an unknown ``section_id`` (mapped to 404 at the
    endpoint layer).
    """
    snapshot = build_snapshot(
        config,
        routing_available=routing_available,
        routing_mounted=routing_mounted,
        content_hashes=content_hashes,
        invalid=invalid,
        raw_values=raw_values,
    )
    for section in snapshot.sections:
        if section.id == section_id:
            return section
    raise KeyError(section_id)


# --- T051: best-effort load of an invalid-on-disk config (research D8) ---------


def load_config_best_effort(
    raw: dict[str, Any],
) -> tuple[CoordinareConfiguration, dict[str, str], dict[str, Any]]:
    """Load a (possibly invalid) raw config, isolating field-level failures.

    Returns ``(config, invalid, raw_values)`` where:
      * ``config`` is a valid :class:`CoordinareConfiguration` with every global
        field that failed validation reset to its schema default (so the
        snapshot can still render structure);
      * ``invalid`` maps the dotted ``global.<field>`` path of each dropped field
        to a humanized error message;
      * ``raw_values`` maps every on-disk global scalar's dotted path to its raw
        value (so invalid fields display what is actually on disk).

    A ``${VAR}`` ``github_token`` is substituted with a placeholder for the
    validation pass only — display values still come from ``raw_values`` (the UI
    never sees the substitute). The substitution is never written back.
    """
    from pydantic import ValidationError

    from coordinare.config import CoordinareConfiguration, ProjectConfiguration
    from coordinare.config_validation import coerce_multi_symphony_raw

    global_field_names = set(ProjectConfiguration.model_fields)
    raw_values: dict[str, Any] = {
        f"global.{k}": v for k, v in raw.items() if k in global_field_names
    }

    attempt = dict(raw)
    token = attempt.get("github_token")
    if is_env_placeholder(token) or token is None:
        # Substitute a non-placeholder token so the pat-auth validator passes;
        # the real on-disk value (incl. ${VAR}) is preserved in raw_values.
        attempt["github_token"] = "ghp_placeholder_for_validation"  # placeholder for the validator, never a real token

    invalid: dict[str, str] = {}
    config: CoordinareConfiguration | None = None
    for _ in range(len(global_field_names) + 1):
        try:
            config = CoordinareConfiguration(**coerce_multi_symphony_raw(attempt))
            break
        except ValidationError as exc:
            removed_any = False
            for err in exc.errors():
                loc = err.get("loc", ())
                # Field-level global errors look like ('global_config', '<field>', ...)
                if len(loc) >= 2 and loc[0] == "global_config":
                    field = loc[1]
                    if not isinstance(field, str) or field not in attempt:
                        continue
                    # Best-effort isolation applies only to *scalar* Global fields,
                    # which render read-only with a banner. Catalogs/collections
                    # (endpoints, model_endpoints, modes, ...) are not in the Global
                    # scalar section, so dropping them would silently remove them
                    # with no UI signal — let those failures re-raise instead.
                    finfo = ProjectConfiguration.model_fields.get(field)
                    if finfo is None or not _is_scalar_field(finfo):
                        continue
                    dotted = f"global.{field}"
                    invalid.setdefault(dotted, err.get("msg", "invalid value"))
                    del attempt[field]
                    removed_any = True
            if not removed_any:
                raise

    if config is None:  # pragma: no cover — loop always converges or re-raises
        raise RuntimeError("could not load config after dropping invalid fields")

    return config, invalid, raw_values
