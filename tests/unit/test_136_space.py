"""Spec 136 — search-space definition: load-time validation, dotted-path
materialization (incl. symphonies-by-name), fingerprints.

Contract: specs/136-board-bench-sweep/contracts/search-space.md
"""

from __future__ import annotations

import copy
from pathlib import Path

import pytest
import yaml

from coordinare.bench.space import (
    SpaceError,
    config_fingerprint,
    load_space,
    materialize,
    resolve_path,
)

# A minimal valid root configuration (global_config + symphonies) with 080
# catalogs so mode dimensions resolve.
BASELINE: dict = {
    "global_config": {
        "github_org": "bench-org",
        "github_token": "bench-placeholder-not-a-secret",
        "human_reviewers": ["reviewer1"],
        "endpoints": [{"name": "ep", "kind": "openai", "auth_env": "K"}],
        "model_endpoints": [
            {"name": "cheap", "endpoint": "ep", "model": "m1"},
            {"name": "premium", "endpoint": "ep", "model": "m2"},
        ],
        "modes": [
            {"name": "single-cheap", "strategy": "single", "tool": "cheap"},
            {"name": "single-premium", "strategy": "single", "tool": "premium"},
        ],
        "performers": {"implementer": {"backend": "opencode", "mode": "single-cheap"}},
    },
    "symphonies": [{"name": "bench", "github_project_number": 1, "persona_scope": {}}],
}

SPACE: dict = {
    "name": "test-space",
    "baseline_config": "baseline.yaml",
    "dimensions": [
        {
            "name": "ci-gate",
            "path": "symphonies.bench.persona_scope.ci_gate.enabled",
            "choices": [False, True],
            "description": "075 gate",
        },
        {
            "name": "implementer-mode",
            "path": "global_config.performers.implementer.mode",
            "choices": ["single-cheap", "single-premium"],
        },
        {"name": "concurrency", "path": "global_config.max_concurrent_cards", "choices": [1, 2]},
    ],
    "candidates": [
        {
            "name": "aggressive",
            "overrides": {
                "symphonies.bench.persona_scope.ci_gate.enabled": True,
                "global_config.max_concurrent_cards": 2,
            },
        },
    ],
}


def _write(tmp_path: Path, space: dict | None = None, baseline: dict | None = None) -> Path:
    (tmp_path / "baseline.yaml").write_text(yaml.safe_dump(baseline or BASELINE))
    space_path = tmp_path / "space.yaml"
    space_path.write_text(yaml.safe_dump(space or SPACE))
    return space_path


class TestLoadSpace:
    def test_valid_space_loads_with_exact_dimensions_and_candidates(self, tmp_path: Path) -> None:
        loaded = load_space(_write(tmp_path))
        assert loaded.definition.name == "test-space"
        assert [d.name for d in loaded.definition.dimensions] == [
            "ci-gate", "implementer-mode", "concurrency",
        ]
        assert [c.name for c in loaded.definition.candidates] == ["aggressive"]
        assert loaded.baseline.global_config.github_org == "bench-org"

    def test_unknown_dimension_path_fails_naming_it(self, tmp_path: Path) -> None:
        bad = copy.deepcopy(SPACE)
        bad["dimensions"][0]["path"] = "symphonies.bench.persona_scope.no_such_gate.enabled"
        with pytest.raises(SpaceError, match="no_such_gate"):
            load_space(_write(tmp_path, space=bad))

    def test_schema_rejected_choice_fails_naming_dimension_and_value(self, tmp_path: Path) -> None:
        bad = copy.deepcopy(SPACE)
        bad["dimensions"][2]["choices"] = [1, 0]  # max_concurrent_cards ge=1
        with pytest.raises(SpaceError, match="concurrency"):
            load_space(_write(tmp_path, space=bad))

    def test_missing_baseline_refuses(self, tmp_path: Path) -> None:
        space_path = tmp_path / "space.yaml"
        space_path.write_text(yaml.safe_dump(SPACE))  # no baseline.yaml written
        with pytest.raises(SpaceError, match="baseline"):
            load_space(space_path)

    def test_invalid_baseline_refuses(self, tmp_path: Path) -> None:
        bad_baseline = {k: v for k, v in BASELINE.items() if k != "global_config"}
        with pytest.raises(SpaceError, match="baseline"):
            load_space(_write(tmp_path, baseline=bad_baseline))

    def test_empty_definition_fails(self, tmp_path: Path) -> None:
        empty = {"name": "x", "baseline_config": "baseline.yaml", "dimensions": [], "candidates": []}
        with pytest.raises(SpaceError, match=r"dimension|candidate"):
            load_space(_write(tmp_path, space=empty))

    def test_candidates_only_definition_is_valid(self, tmp_path: Path) -> None:
        cand_only = copy.deepcopy(SPACE)
        cand_only["dimensions"] = []
        loaded = load_space(_write(tmp_path, space=cand_only))
        assert loaded.definition.dimensions == []
        assert len(loaded.definition.candidates) == 1

    def test_unknown_symphony_name_in_path_fails(self, tmp_path: Path) -> None:
        bad = copy.deepcopy(SPACE)
        bad["dimensions"][0]["path"] = "symphonies.ghost.persona_scope.ci_gate.enabled"
        with pytest.raises(SpaceError, match="ghost"):
            load_space(_write(tmp_path, space=bad))

    def test_duplicate_choices_in_a_dimension_are_rejected(self, tmp_path: Path) -> None:
        bad = copy.deepcopy(SPACE)
        bad["dimensions"][2]["choices"] = [1, 2, 2]  # would silently double-run the point
        with pytest.raises(SpaceError, match="duplicate choice"):
            load_space(_write(tmp_path, space=bad))

    def test_invalid_candidate_override_fails_naming_candidate(self, tmp_path: Path) -> None:
        bad = copy.deepcopy(SPACE)
        bad["candidates"][0]["overrides"]["global_config.max_concurrent_cards"] = 0
        with pytest.raises(SpaceError, match="aggressive"):
            load_space(_write(tmp_path, space=bad))


class TestMaterialize:
    def test_override_reaches_the_config_object(self, tmp_path: Path) -> None:
        loaded = load_space(_write(tmp_path))
        cfg = materialize(loaded.baseline_dump, {"global_config.max_concurrent_cards": 2})
        assert cfg.global_config.max_concurrent_cards == 2
        assert loaded.baseline.global_config.max_concurrent_cards == 1  # baseline untouched

    def test_symphonies_by_name_override(self, tmp_path: Path) -> None:
        loaded = load_space(_write(tmp_path))
        cfg = materialize(
            loaded.baseline_dump,
            {"symphonies.bench.persona_scope.ci_gate.enabled": True},
        )
        assert cfg.symphonies[0].persona_scope is not None
        assert cfg.symphonies[0].persona_scope.ci_gate.enabled is True

    def test_rejected_value_raises_naming_path_and_value(self, tmp_path: Path) -> None:
        loaded = load_space(_write(tmp_path))
        with pytest.raises(SpaceError, match="max_concurrent_cards"):
            materialize(loaded.baseline_dump, {"global_config.max_concurrent_cards": 0})

    def test_resolve_path_reads_baseline_value(self, tmp_path: Path) -> None:
        loaded = load_space(_write(tmp_path))
        assert resolve_path(loaded.baseline_dump, "global_config.max_concurrent_cards") == 1
        assert (
            resolve_path(loaded.baseline_dump, "global_config.performers.implementer.mode")
            == "single-cheap"
        )


class TestFingerprints:
    def test_distinct_points_have_distinct_fingerprints(self, tmp_path: Path) -> None:
        loaded = load_space(_write(tmp_path))
        a = config_fingerprint(materialize(loaded.baseline_dump, {"global_config.max_concurrent_cards": 1}))
        b = config_fingerprint(materialize(loaded.baseline_dump, {"global_config.max_concurrent_cards": 2}))
        assert a != b
        assert len(a) == 16

    def test_configs_differing_only_in_a_secret_get_distinct_fingerprints(
        self, tmp_path: Path
    ) -> None:
        loaded = load_space(_write(tmp_path))
        a = config_fingerprint(
            materialize(loaded.baseline_dump, {"global_config.github_token": "secret-one"})
        )
        b = config_fingerprint(
            materialize(loaded.baseline_dump, {"global_config.github_token": "secret-two"})
        )
        assert a != b  # SecretStr masking must not collapse distinct points (FR-004)

    def test_identical_materializations_share_a_fingerprint(self, tmp_path: Path) -> None:
        loaded = load_space(_write(tmp_path))
        a = config_fingerprint(materialize(loaded.baseline_dump, {"global_config.max_concurrent_cards": 2}))
        b = config_fingerprint(materialize(loaded.baseline_dump, {"global_config.max_concurrent_cards": 2}))
        assert a == b


class TestShippedDefaultSpace:
    def test_default_space_loads_clean(self) -> None:
        loaded = load_space(Path("benchmarks/spaces/default.yaml"))
        assert loaded.definition.dimensions, "default space must declare swept dimensions"
        assert loaded.definition.candidates, "default space must declare candidates"
