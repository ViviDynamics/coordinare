"""Spec 161 — harness sweep dimension and its load-time validation.

T018 (US2): SUPPORTED_BACKENDS is importable and module-level (R8).
T019 (US2): get_backend behavior is unchanged by the extraction (R8 regression pin).
T021 (US2): a harness dimension expands to one point per choice (FR-014, FR-015).
T022 (US2): each point materializes to a schema-valid configuration (FR-016).
T023 (US2): an unknown harness name is rejected at load time (FR-017).
"""

from __future__ import annotations

import pytest
import yaml
from performer.backends import SUPPORTED_BACKENDS, UnsupportedBackendError, get_backend

from coordinare.bench.space import (
    Dimension,
    SearchSpace,
    SpaceError,
    load_space,
    materialize,
    resolve_path,
)

BASELINE = "benchmarks/spaces/baseline.yaml"


@pytest.fixture
def baseline_dump() -> dict:
    return yaml.safe_load(open(BASELINE).read())


def _space_file(tmp_path, dimensions: list[dict]) -> str:
    """Write a space definition next to a copy of the real baseline."""
    import shutil

    shutil.copy(BASELINE, tmp_path / "baseline.yaml")
    path = tmp_path / "space.yaml"
    path.write_text(
        yaml.safe_dump({
            "name": "harness-test",
            "baseline_config": "baseline.yaml",
            "dimensions": dimensions,
        }),
    )
    return str(path)


# --- T018: the constant exists and is module-level -----------------------------------


def test_supported_backends_is_importable_and_module_level() -> None:
    """R8: it used to be a function-local dict that only the docstring mentioned."""
    assert isinstance(SUPPORTED_BACKENDS, dict)
    assert SUPPORTED_BACKENDS, "the harness set must not be empty"


def test_supported_backends_contains_the_known_harnesses() -> None:
    assert set(SUPPORTED_BACKENDS) == {
        "opencode",
        "opencode_compat",
        "junie",
        "claude_code",
        "codex",
        "hermes",
        "pi",
        "prime_agent",
        "openclaw",
        # 521: the driver adapter (coordinare-driver integration contract).
        "driver",
    }


# --- T019: the extraction changed no behavior ----------------------------------------


def test_get_backend_still_returns_an_adapter_for_a_valid_name() -> None:
    """R8 regression pin: promoting the dict must not change the factory."""
    assert type(get_backend("claude_code")).__name__ == "ClaudeCodeBackend"


def test_get_backend_still_raises_for_an_unknown_name() -> None:
    with pytest.raises(UnsupportedBackendError, match="unsupported backend"):
        get_backend("not_a_harness")


def test_get_backend_error_still_lists_the_supported_names() -> None:
    """The message shape callers rely on is unchanged."""
    with pytest.raises(UnsupportedBackendError) as exc:
        get_backend("not_a_harness")
    for name in SUPPORTED_BACKENDS:
        assert name in str(exc.value)


# --- T021 / T022: expansion and materialization --------------------------------------


def test_role_shorthand_normalizes_to_the_backend_path() -> None:
    """FR-014: every consumer keys on `path`, so `role` is normalized at load."""
    dim = Dimension(name="reviewer-harness", role="reviewer", choices=["openclaw"])
    assert dim.path == "global_config.performers.reviewer.backend"


def test_path_and_role_are_mutually_exclusive() -> None:
    """Two ways to name one target would let them disagree."""
    with pytest.raises(ValueError, match="exactly one of 'path' or 'role'"):
        Dimension(
            name="d", role="reviewer", path="global_config.performers.qa.backend",
            choices=["openclaw"],
        )
    with pytest.raises(ValueError, match="exactly one of 'path' or 'role'"):
        Dimension(name="d", choices=["openclaw"])


def test_a_role_dimension_survives_a_serialization_round_trip() -> None:
    """The validator normalizes role -> path. If it leaves `role` populated, the dumped
    model carries BOTH and fails its own exactly-one-of check on the way back in.

    That breaks anything that dumps and reloads a space: caching, the sweep's config
    fingerprinting path, or simply persisting a definition.
    """
    dim = Dimension(name="reviewer-harness", role="reviewer", choices=["openclaw"])
    restored = Dimension.model_validate_json(dim.model_dump_json())

    assert restored.path == "global_config.performers.reviewer.backend"


def test_a_role_dimension_survives_model_copy() -> None:
    copied = Dimension(name="d", role="reviewer", choices=["openclaw"]).model_copy()
    assert Dimension.model_validate(copied.model_dump()).path.endswith(".backend")


def test_a_whole_space_survives_a_round_trip() -> None:
    """The nested case: SearchSpace -> JSON -> SearchSpace."""
    space = SearchSpace(
        name="s", baseline_config="baseline.yaml",
        dimensions=[{"name": "d", "role": "reviewer", "choices": ["openclaw"]}],
    )
    restored = SearchSpace.model_validate_json(space.model_dump_json())

    assert restored.dimensions[0].path == "global_config.performers.reviewer.backend"


def test_harness_dimension_loads_and_expands(tmp_path) -> None:
    """FR-014/FR-015: one point per declared harness."""
    space = load_space(
        _space_file(tmp_path, [
            {"name": "reviewer-harness", "role": "reviewer",
             "choices": ["openclaw", "claude_code", "codex"]},
        ]),
    )
    dim = space.definition.dimensions[0]

    assert dim.path == "global_config.performers.reviewer.backend"
    assert dim.choices == ["openclaw", "claude_code", "codex"]


def test_each_choice_materializes_and_sets_only_that_role(baseline_dump) -> None:
    """FR-015/FR-016: the swept role changes; everything else is held fixed.

    `security` is the untouched control here because the benchmark baseline declares only
    implementer/reviewer/security.
    """
    other_before = resolve_path(baseline_dump, "global_config.performers.security.backend")

    cfg = materialize(baseline_dump, {"global_config.performers.reviewer.backend": "junie"})

    assert cfg.global_config.performers.reviewer.backend == "junie"
    assert cfg.global_config.performers.security.backend == other_before
    assert other_before != "junie", "control role must differ, or the test proves nothing"


@pytest.mark.parametrize("harness", sorted(SUPPORTED_BACKENDS))
def test_every_supported_harness_materializes(baseline_dump, harness: str) -> None:
    """FR-016: no supported harness is rejected by the config schema."""
    cfg = materialize(
        baseline_dump, {"global_config.performers.reviewer.backend": harness},
    )
    assert cfg.global_config.performers.reviewer.backend == harness


# --- T023: unknown harness and unknown role ------------------------------------------


def test_unknown_harness_name_is_rejected_at_load_time(tmp_path) -> None:
    """FR-017: the config schema does NOT constrain `backend`, so nothing else catches a
    typo — it would expand into a point, run, and fail partway through a long sweep."""
    with pytest.raises(SpaceError, match="unknown harness"):
        load_space(
            _space_file(tmp_path, [
                {"name": "reviewer-harness", "role": "reviewer",
                 "choices": ["openclaw", "openclw"]},
            ]),
        )


def test_unknown_harness_error_names_the_offending_value(tmp_path) -> None:
    with pytest.raises(SpaceError) as exc:
        load_space(
            _space_file(tmp_path, [
                {"name": "d", "role": "reviewer", "choices": ["openclw"]},
            ]),
        )
    assert "openclw" in str(exc.value)


def test_the_bare_schema_still_accepts_an_unknown_harness(baseline_dump) -> None:
    """Pins WHY FR-017 exists. If this ever starts raising, the config schema gained its
    own constraint and the space-level check becomes belt-and-braces rather than the only
    guard. Asserting the current reality keeps the rationale honest."""
    cfg = materialize(
        baseline_dump, {"global_config.performers.reviewer.backend": "not_a_harness"},
    )
    assert cfg.global_config.performers.reviewer.backend == "not_a_harness"


def test_unknown_role_is_rejected_by_the_existing_path_walk(tmp_path) -> None:
    """FR-017's role half already held before this feature (R2)."""
    with pytest.raises(SpaceError, match="does not resolve in the baseline"):
        load_space(
            _space_file(tmp_path, [
                {"name": "d", "role": "nosuchrole", "choices": ["openclaw"]},
            ]),
        )


def test_a_non_harness_dimension_is_not_harness_validated(tmp_path) -> None:
    """The harness check must not leak onto unrelated dimensions."""
    space = load_space(
        _space_file(tmp_path, [
            {"name": "concurrency", "path": "global_config.max_concurrent_cards",
             "choices": [1, 2]},
        ]),
    )
    assert space.definition.dimensions[0].choices == [1, 2]
