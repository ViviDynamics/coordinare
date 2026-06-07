"""Foundational unit tests for the config descriptor layer (spec 081-config-ui).

Covers T010 foundational concerns: secret masking, ${VAR} literal preservation,
and pydantic-model introspection into ConfigSetting descriptors (T006/T007).
Section grouping / CatalogItem projection tests (T012/T051) are added in Phase 3.
"""

from __future__ import annotations

from coordinare import config_descriptors as cd
from coordinare.config import Endpoint, Mode, ProjectConfiguration

# --- T007: secret masking + ${VAR} preservation -------------------------------


def test_serialize_value_masks_secret_literal():
    display, is_env = cd.serialize_value("ghp_realsecret", secret=True)
    assert display == cd.SECRET_MASK
    assert is_env is False


def test_serialize_value_preserves_env_placeholder_even_when_secret():
    display, is_env = cd.serialize_value("${COORDINARE_GITHUB_TOKEN}", secret=True)
    assert display == "${COORDINARE_GITHUB_TOKEN}"
    assert is_env is True


def test_serialize_value_non_secret_passes_through():
    display, is_env = cd.serialize_value(30, secret=False)
    assert display == 30
    assert is_env is False


def test_serialize_value_none_secret_not_masked():
    # An unset secret has nothing to hide; masking an absent value would imply
    # a value exists. Return None unchanged.
    display, is_env = cd.serialize_value(None, secret=True)
    assert display is None
    assert is_env is False


def test_is_env_placeholder_detection():
    assert cd.is_env_placeholder("${FOO}") is True
    assert cd.is_env_placeholder("${FOO}/bar") is True
    assert cd.is_env_placeholder("plain") is False
    assert cd.is_env_placeholder(42) is False


def test_is_env_placeholder_rejects_non_placeholder_brace_chars():
    """A secret literal that merely contains ``${`` and ``}`` but no real
    ``${VARNAME}`` placeholder must NOT be mistaken for an env reference — else
    serialize_value would pass it through verbatim instead of masking it,
    leaking the secret (spec 081: never expose secrets).
    """
    # ``${`` and ``}`` present, but not as a contiguous ``${VARNAME}`` token.
    assert cd.is_env_placeholder("}literal text containing ${") is False
    assert cd.is_env_placeholder("${ has space }") is False
    assert cd.is_env_placeholder("${}") is False  # empty name
    assert cd.is_env_placeholder("${1FOO}") is False  # must start with letter/_
    # Genuine placeholders (incl. with prefix/suffix) still detected.
    assert cd.is_env_placeholder("prefix-${FOO}") is True
    assert cd.is_env_placeholder("${FOO_BAR2}") is True


# --- T005: annotation table ---------------------------------------------------


def test_github_token_flagged_secret():
    ann = cd.annotation_for("global.github_token")
    assert ann.secret is True


def test_auth_env_is_not_a_secret():
    # auth_env holds the NAME of an env var, never the secret itself.
    ann = cd.annotation_for("endpoints.auth_env")
    assert ann.secret is False


def test_binding_field_is_restart_required():
    assert cd.annotation_for("global.dashboard_port").restart_required is True


def test_hot_reloadable_field_not_restart_required():
    assert cd.annotation_for("global.poll_interval_seconds").restart_required is False


def test_unknown_key_falls_back_to_default_annotation():
    ann = cd.annotation_for("global.some_unknown_field_xyz")
    assert ann.editable is True
    assert ann.restart_required is False
    assert ann.secret is False


# --- T006: pydantic introspection into ConfigSetting --------------------------


def test_setting_from_field_infers_int_type_and_range():
    fi = ProjectConfiguration.model_fields["poll_interval_seconds"]
    setting = cd.setting_from_field("global", "poll_interval_seconds", fi, 30)
    assert setting.type == "int"
    assert setting.range == {"min": 0, "max": 3600}
    assert setting.current_value == 30
    assert setting.key == "global.poll_interval_seconds"
    assert setting.editable is True


def test_setting_from_field_infers_enum_from_literal():
    fi = Endpoint.model_fields["kind"]
    setting = cd.setting_from_field("endpoints", "kind", fi, "vllm")
    assert setting.type == "enum"
    assert set(setting.enum) == {"litellm", "ollama", "vllm", "openai", "anthropic"}


def test_setting_from_field_infers_enum_from_strenum():
    from coordinare.config import BranchCollisionStrategy

    fi = ProjectConfiguration.model_fields["branch_collision_strategy"]
    setting = cd.setting_from_field(
        "global", "branch_collision_strategy", fi, BranchCollisionStrategy.delete
    )
    assert setting.type == "enum"
    assert set(setting.enum) == {s.value for s in BranchCollisionStrategy}


def test_setting_from_field_masks_secret_current_value():
    fi = ProjectConfiguration.model_fields["github_token"]
    setting = cd.setting_from_field("global", "github_token", fi, "ghp_realsecret")
    assert setting.secret is True
    assert setting.current_value == cd.SECRET_MASK


def test_setting_from_field_env_placeholder_not_masked():
    fi = ProjectConfiguration.model_fields["github_token"]
    setting = cd.setting_from_field(
        "global", "github_token", fi, "${COORDINARE_GITHUB_TOKEN}"
    )
    assert setting.is_env_placeholder is True
    assert setting.current_value == "${COORDINARE_GITHUB_TOKEN}"


def test_setting_from_field_float_threshold_range():
    fi = Mode.model_fields["threshold"]
    setting = cd.setting_from_field("modes", "threshold", fi, 0.5)
    assert setting.type == "float"
    assert setting.range == {"min": 0.0, "max": 1.0}


def test_setting_from_field_bool_type():
    fi = ProjectConfiguration.model_fields["stale_branch_cleanup"]
    setting = cd.setting_from_field("global", "stale_branch_cleanup", fi, True)
    assert setting.type == "bool"


def test_setting_from_field_infers_string_length_range():
    # String fields use min_length/max_length (annotated_types.MinLen/MaxLen),
    # which the descriptor must surface so the UI can show the allowed range.
    from coordinare.config import ScopeTierBehavior

    fi = ScopeTierBehavior.model_fields["prompt_addon"]
    setting = cd.setting_from_field("modes", "prompt_addon", fi, "")
    assert setting.type == "string"
    assert setting.range == {"max_length": 4096}


def test_setting_from_field_populates_help_from_description():
    # ConfigSetting.help must be sourced from the pydantic Field(description=...)
    # so the UI can show operator-facing help text for each setting.
    from pydantic import BaseModel, Field

    class _M(BaseModel):
        widget_count: int = Field(0, description="How many widgets to allocate.")

    fi = _M.model_fields["widget_count"]
    setting = cd.setting_from_field("global", "widget_count", fi, 0)
    assert setting.help == "How many widgets to allocate."


def test_infer_range_marks_exclusive_bounds():
    # gt/lt are EXCLUSIVE bounds. Collapsing them into a bare min/max would let the
    # descriptor claim e.g. 0.0 is allowed when the schema requires > 0.0, making the
    # UI range inconsistent with server validation. They must be flagged exclusive.
    from pydantic import BaseModel, Field

    class _M(BaseModel):
        ratio: float = Field(0.5, gt=0.0, le=1.0)

    fi = _M.model_fields["ratio"]
    rng = cd._infer_range(fi)
    assert rng == {"min": 0.0, "max": 1.0, "min_exclusive": True}


def test_infer_range_inclusive_bounds_have_no_exclusive_flag():
    from pydantic import BaseModel, Field

    class _M(BaseModel):
        count: int = Field(5, ge=1, le=10)

    fi = _M.model_fields["count"]
    rng = cd._infer_range(fi)
    assert rng == {"min": 1, "max": 10}


def test_setting_from_field_help_is_none_without_description():
    from pydantic import BaseModel

    class _M(BaseModel):
        plain: int = 0

    fi = _M.model_fields["plain"]
    setting = cd.setting_from_field("global", "plain", fi, 0)
    assert setting.help is None


def test_setting_from_field_default_factory_is_resolved():
    # Fields using default_factory have FieldInfo.default == PydanticUndefined;
    # the descriptor must call the factory so the UI shows the real default.
    fi = ProjectConfiguration.model_fields["trusted_bot_reviewers"]
    setting = cd.setting_from_field("global", "trusted_bot_reviewers", fi, [])
    assert setting.default == []


# --- T012: section grouping + ConfigSnapshot assembly (data-model E2/E5) ------

SECTION_IDS = {
    "global",
    "personas",
    "symphonies",
    "endpoints",
    "model_endpoints",
    "modes",
    "routing",
}


def _sections_by_id(snapshot: cd.ConfigSnapshot) -> dict[str, cd.ConfigSection]:
    return {s.id: s for s in snapshot.sections}


def test_build_snapshot_has_all_seven_sections(coordinare_config):
    snapshot = cd.build_snapshot(coordinare_config, routing_available=True)
    assert {s.id for s in snapshot.sections} == SECTION_IDS


def test_build_snapshot_global_is_scalar_group_with_known_fields(coordinare_config):
    snapshot = cd.build_snapshot(coordinare_config, routing_available=True)
    global_section = _sections_by_id(snapshot)["global"]
    assert global_section.kind == "scalar_group"
    keys = {s.key for s in global_section.settings}
    assert "global.poll_interval_seconds" in keys
    assert "global.github_token" in keys
    # Catalogs live in their own collection sections, not under global.
    assert "global.endpoints" not in keys
    assert "global.personas" not in keys


def test_build_snapshot_global_github_token_env_placeholder_not_masked(coordinare_config):
    # Display values come from the raw on-disk YAML (research D3): a ${VAR}
    # literal is preserved verbatim and never masked, so an operator can see
    # which env var supplies the secret.
    snapshot = cd.build_snapshot(
        coordinare_config,
        routing_available=True,
        raw_values={"global.github_token": "${COORDINARE_GITHUB_TOKEN}"},
    )
    global_section = _sections_by_id(snapshot)["global"]
    token = next(s for s in global_section.settings if s.key == "global.github_token")
    assert token.is_env_placeholder is True
    assert token.current_value == "${COORDINARE_GITHUB_TOKEN}"
    assert token.current_value != cd.SECRET_MASK


def test_catalog_sections_are_collections(coordinare_config):
    snapshot = cd.build_snapshot(coordinare_config, routing_available=True)
    by_id = _sections_by_id(snapshot)
    for cat in ("endpoints", "model_endpoints", "modes", "personas", "symphonies"):
        assert by_id[cat].kind == "collection"


def test_referenced_endpoint_is_not_deletable(coordinare_config):
    snapshot = cd.build_snapshot(coordinare_config, routing_available=True)
    endpoints = _sections_by_id(snapshot)["endpoints"]
    local = next(i for i in endpoints.items if i.id == "local-vllm")
    # qwen-coder model_endpoint references endpoint local-vllm.
    assert "qwen-coder" in local.referenced_by
    assert local.deletable is False


def test_unreferenced_endpoint_is_deletable(coordinare_config):
    snapshot = cd.build_snapshot(coordinare_config, routing_available=True)
    endpoints = _sections_by_id(snapshot)["endpoints"]
    native = next(i for i in endpoints.items if i.id == "openai-native")
    assert native.referenced_by == []
    assert native.deletable is True


def test_referenced_model_endpoint_is_not_deletable(coordinare_config):
    snapshot = cd.build_snapshot(coordinare_config, routing_available=True)
    model_endpoints = _sections_by_id(snapshot)["model_endpoints"]
    qwen = next(i for i in model_endpoints.items if i.id == "qwen-coder")
    # single-qwen mode references model_endpoint qwen-coder via tool.
    assert "single-qwen" in qwen.referenced_by
    assert qwen.deletable is False


# --- T051: invalid-on-disk handling (research D8) -----------------------------


def test_best_effort_load_marks_invalid_field_read_only():
    raw = {
        "github_org": "acme",
        "github_token": "${COORDINARE_GITHUB_TOKEN}",
        "human_reviewers": ["alice"],
        "poll_interval_seconds": 999999,  # out of range (le=3600)
        "symphonies": [{"name": "demo", "github_project_number": 100}],
        "orchestra": {"mode": "shared_pool"},
    }
    config, invalid, raw_values = cd.load_config_best_effort(raw)
    assert config is not None
    # The offending field is recorded as invalid, keyed by dotted path.
    assert "global.poll_interval_seconds" in invalid
    assert raw_values["global.poll_interval_seconds"] == 999999

    snapshot = cd.build_snapshot(
        config,
        routing_available=False,
        invalid=invalid,
        raw_values=raw_values,
    )
    global_section = _sections_by_id(snapshot)["global"]
    bad = next(
        s for s in global_section.settings if s.key == "global.poll_interval_seconds"
    )
    assert bad.invalid is True
    assert bad.editable is False
    assert bad.current_value == 999999
    assert global_section.invalid_banner is not None
    # Valid fields still render normally.
    org = next(s for s in global_section.settings if s.key == "global.github_org")
    assert org.invalid is False
    assert org.editable is True


def test_best_effort_load_does_not_silently_drop_invalid_catalog():
    # Best-effort load only isolates *scalar* Global fields (which render
    # read-only with a banner). A non-scalar catalog/collection (endpoints,
    # model_endpoints, modes, ...) is NOT rendered in the Global scalar section,
    # so dropping it would silently remove it from the loaded config with no UI
    # signal. Such a failure must re-raise instead of being swallowed.
    import pytest
    from pydantic import ValidationError

    raw = {
        "github_org": "acme",
        "github_token": "${COORDINARE_GITHUB_TOKEN}",
        "human_reviewers": ["alice"],
        "symphonies": [{"name": "demo", "github_project_number": 100}],
        "orchestra": {"mode": "shared_pool"},
        # Invalid endpoint entry (kind is not a valid Literal) → catalog-level
        # ValidationError. Must NOT be silently dropped.
        "endpoints": [{"id": "bad", "kind": "not_a_real_kind"}],
    }
    with pytest.raises(ValidationError):
        cd.load_config_best_effort(raw)


def test_build_snapshot_routing_unavailable_empty_state(coordinare_config):
    snapshot = cd.build_snapshot(coordinare_config, routing_available=False)
    routing = _sections_by_id(snapshot)["routing"]
    assert routing.items == []
    assert routing.invalid_banner is not None
    assert snapshot.routing_available is False


def test_routing_banner_unmounted_says_not_mounted(coordinare_config):
    """No endpoint mounts a routing path → banner advises mounting one."""
    snapshot = cd.build_snapshot(
        coordinare_config, routing_available=False, routing_mounted=False
    )
    routing = _sections_by_id(snapshot)["routing"]
    assert "is mounted" in routing.invalid_banner
    assert "Mount a routing YAML" in routing.invalid_banner


def test_routing_banner_mounted_but_missing_says_check_mount(coordinare_config):
    """An endpoint mounts a routing path but the host file is missing/not a file
    → banner must NOT claim 'no routing table is mounted' (FR misdiagnosis)."""
    snapshot = cd.build_snapshot(
        coordinare_config, routing_available=False, routing_mounted=True
    )
    routing = _sections_by_id(snapshot)["routing"]
    assert routing.invalid_banner is not None
    # Must not misdiagnose a present-but-broken mount as "unmounted".
    assert "No performer routing table is mounted" not in routing.invalid_banner
    assert "mounted" in routing.invalid_banner
    assert (
        "missing" in routing.invalid_banner
        or "not a regular file" in routing.invalid_banner
    )
