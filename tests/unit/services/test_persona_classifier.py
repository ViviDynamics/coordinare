"""074 T014 — Unit tests for ``_match_path_classes``.

Locks the matching semantics referenced by R8: globs are evaluated in
``path_classes`` insertion order, each class contributes at most once (first
matching pattern wins for that class), and malformed / empty inputs are
skipped silently.
"""
from __future__ import annotations

from typing import Any

import pytest

from coordinare.services import persona_classifier
from coordinare.services.persona_classifier import _match_path_classes


def test_single_class_match() -> None:
    classes = {"docs": ["*.md", "docs/**"]}
    assert _match_path_classes("README.md", classes) == ["docs"]


def test_multi_class_match_preserves_insertion_order() -> None:
    classes = {
        "docs": ["*.md"],
        "tests": ["tests/*"],
        "src": ["src/*"],
    }
    # tests/foo.md matches docs (via *.md) and tests (via tests/*)
    assert _match_path_classes("tests/foo.md", classes) == ["docs", "tests"]


def test_no_match_returns_empty() -> None:
    classes = {"docs": ["*.md"], "tests": ["tests/*"]}
    assert _match_path_classes("src/coordinare/daemon.py", classes) == []


def test_class_contributes_only_once_even_with_multiple_matching_globs() -> None:
    classes = {"docs": ["*.md", "*.MD", "README*"]}
    # All three globs match README.md but the class appears once.
    assert _match_path_classes("README.md", classes) == ["docs"]


def test_malformed_patterns_are_skipped() -> None:
    classes = {
        "docs": ["", None, 42, "*.md"],  # type: ignore[list-item]
        "broken": "not-a-list",  # type: ignore[dict-item]
        "empty": [],
    }
    assert _match_path_classes("README.md", classes) == ["docs"]


def test_empty_inputs_return_empty() -> None:
    assert _match_path_classes("", {"docs": ["*.md"]}) == []
    assert _match_path_classes("README.md", {}) == []


# ---------------------------------------------------------------------------
# T036 / T037 — forced_full_on_path_classes (US2)
# ---------------------------------------------------------------------------


class _FakeBackend:
    def __init__(self, response: dict[str, Any]) -> None:
        self._response = response
        self.calls: list[dict[str, Any]] = []

    async def prompt(self, body: str, response_format: str | None = None) -> dict[str, Any]:
        self.calls.append({"body": body, "response_format": response_format})
        return self._response


class _FakeGitHub:
    def __init__(self, files: list[dict[str, Any]], head_sha: str = "abc123") -> None:
        self._files = files
        self._head_sha = head_sha

    async def get_pr_files(self, owner: str, repo: str, pr_number: int) -> dict[str, Any]:
        return {"files": list(self._files), "head_sha": self._head_sha}


class _ScopeCfg:
    def __init__(self, *, forced_full_on_path_classes: dict[str, list[str]] | None = None) -> None:
        self.enabled = True
        self.path_classes = {
            "docs": ["*.md"],
            "runtime": ["src/**/*.py"],
            "security_sensitive": ["src/auth/*.py"],
        }
        self.classifier_latency_budget_seconds = 30.0
        self.forced_full_on_path_classes = forced_full_on_path_classes or {}


class _Cfg:
    def __init__(self, **kw: Any) -> None:
        self.persona_scope = _ScopeCfg(**kw)


def _card() -> dict[str, Any]:
    return {
        "id": "PVI_42",
        "title": "Touch auth",
        "body": "small auth change",
        "pr_url": "https://github.com/acme/repo/pull/91",
    }


def _baseline_response() -> dict[str, Any]:
    # Classifier says security should only skim — forced_full must override.
    return {
        "data": {
            "personas": {
                "reviewer": {"depth": "normal", "focus": "ok"},
                "security": {"depth": "skim", "focus": "looks small"},
                "qa": {"depth": "normal", "focus": "ok"},
                "tech_writer": {"depth": "skip", "focus": "no docs"},
                "closer": {"depth": "skim", "focus": "ok"},
            },
        },
    }


@pytest.mark.asyncio
async def test_forced_full_override_applied() -> None:
    """T036 — When a touched file belongs to a forced-full class for a persona,
    that persona's depth flips to full and overrides records the tag.
    """
    backend = _FakeBackend(_baseline_response())
    gh = _FakeGitHub([
        {"path": "src/auth/session.py", "added": 5, "removed": 1, "status": "modified"},
    ])
    cfg = _Cfg(forced_full_on_path_classes={"security": ["security_sensitive"]})

    scope = await persona_classifier.classify(
        session={"feedback_cycle_count": 0},
        card=_card(),
        conducting_backend=backend,
        config=cfg,
        github_service=gh,
        classifier_model="test/model",
    )
    assert scope is not None
    security = scope["personas"]["security"]
    assert security["depth"] == "full"
    assert "forced_full_on_path_class:security_sensitive" in security["overrides"]


@pytest.mark.asyncio
async def test_forced_full_does_not_affect_other_personas() -> None:
    """T037 — FR-005: forced_full is surgical.  Other personas keep what the
    classifier emitted.
    """
    backend = _FakeBackend(_baseline_response())
    gh = _FakeGitHub([
        {"path": "src/auth/session.py", "added": 5, "removed": 1, "status": "modified"},
    ])
    cfg = _Cfg(forced_full_on_path_classes={"security": ["security_sensitive"]})

    scope = await persona_classifier.classify(
        session={"feedback_cycle_count": 0},
        card=_card(),
        conducting_backend=backend,
        config=cfg,
        github_service=gh,
        classifier_model="test/model",
    )
    assert scope is not None
    personas = scope["personas"]
    assert personas["reviewer"]["depth"] == "normal"
    assert personas["qa"]["depth"] == "normal"
    assert personas["tech_writer"]["depth"] == "skip"
    for name in ("reviewer", "qa", "tech_writer"):
        assert not any(
            o.startswith("forced_full_on_path_class:")
            for o in personas[name].get("overrides", [])
        )


# ---------------------------------------------------------------------------
# T040-T044 — classifier failure fallback (US3)
# ---------------------------------------------------------------------------


class _RaisingBackend:
    def __init__(self, exc: BaseException) -> None:
        self._exc = exc

    async def prompt(self, body: str, response_format: str | None = None) -> dict[str, Any]:
        raise self._exc


class _SlowBackend:
    async def prompt(self, body: str, response_format: str | None = None) -> dict[str, Any]:
        import asyncio as _asyncio
        await _asyncio.sleep(5.0)
        return {}


class _CustomResponseBackend:
    def __init__(self, response: Any) -> None:
        self._response = response

    async def prompt(self, body: str, response_format: str | None = None) -> Any:
        return self._response


@pytest.fixture(autouse=True)
def _clear_cooldown() -> Any:
    persona_classifier.reset_failure_warning_cooldown()
    yield
    persona_classifier.reset_failure_warning_cooldown()


def _assert_full_everywhere(scope: dict[str, Any], reason: str) -> None:
    personas = scope["personas"]
    for name in persona_classifier.CLASSIFIER_PERSONAS:
        assert name in personas
        slice_ = personas[name]
        if name == "closer":
            # closer-invariant runs last; depth=full + invariant tag retained
            assert slice_["depth"] == "full"
            assert "closer_is_scope_invariant" in slice_["overrides"]
        else:
            assert slice_["depth"] == "full"
            assert any(o == f"classifier_failed:{reason}" for o in slice_["overrides"])


@pytest.mark.asyncio
async def test_timeout_falls_back_to_full() -> None:
    """T040 — Backend timeout → full-everywhere fallback with reason=timeout."""
    gh = _FakeGitHub([{"path": "README.md", "added": 3, "removed": 0, "status": "modified"}])
    cfg = _Cfg()
    cfg.persona_scope.classifier_latency_budget_seconds = 0.05  # force timeout

    scope = await persona_classifier.classify(
        session={"feedback_cycle_count": 0},
        card=_card(),
        conducting_backend=_SlowBackend(),
        config=cfg,
        github_service=gh,
        classifier_model="test/model",
    )
    assert scope is not None
    _assert_full_everywhere(scope, "timeout")


@pytest.mark.asyncio
async def test_malformed_json_falls_back_to_full() -> None:
    """T041 — Backend returns non-dict → parse_failed fallback."""
    gh = _FakeGitHub([{"path": "README.md", "added": 3, "removed": 0, "status": "modified"}])
    backend = _CustomResponseBackend("not a dict — string body")

    scope = await persona_classifier.classify(
        session={"feedback_cycle_count": 0},
        card=_card(),
        conducting_backend=backend,
        config=_Cfg(),
        github_service=gh,
        classifier_model="test/model",
    )
    assert scope is not None
    _assert_full_everywhere(scope, "parse_failed")


@pytest.mark.asyncio
async def test_unknown_depth_value_falls_back_to_full() -> None:
    """T042 — All slices reject unknown depths → all_slices_invalid fallback (R6)."""
    gh = _FakeGitHub([{"path": "README.md", "added": 3, "removed": 0, "status": "modified"}])
    backend = _CustomResponseBackend({
        "data": {
            "personas": {
                "reviewer": {"depth": "deep", "focus": "x"},
                "security": {"depth": "deep", "focus": "x"},
                "qa": {"depth": "deep", "focus": "x"},
                "tech_writer": {"depth": "deep", "focus": "x"},
                "closer": {"depth": "deep", "focus": "x"},
            },
        },
    })

    scope = await persona_classifier.classify(
        session={"feedback_cycle_count": 0},
        card=_card(),
        conducting_backend=backend,
        config=_Cfg(),
        github_service=gh,
        classifier_model="test/model",
    )
    assert scope is not None
    _assert_full_everywhere(scope, "all_slices_invalid")


@pytest.mark.asyncio
async def test_previous_cycle_scope_reused_on_transient_failure() -> None:
    """Prior session.persona_scope present → reused on failure with refreshed cycle bookkeeping."""
    prior_scope = {
        "computed_at": "2026-05-26T00:00:00Z",
        "cycle_index": 0,
        "classifier_model": "prev/model",
        "head_sha": "cafebabe",
        "files_summary": [],
        "personas": {
            "reviewer": {"depth": "skim", "focus": "prior", "overrides": []},
            "security": {"depth": "skip", "focus": "prior", "overrides": []},
            "qa": {"depth": "skip", "focus": "prior", "overrides": []},
            "tech_writer": {"depth": "full", "focus": "prior", "overrides": []},
            "closer": {"depth": "full", "focus": "prior", "overrides": ["closer_is_scope_invariant"]},
        },
    }
    gh = _FakeGitHub([{"path": "README.md", "added": 3, "removed": 0, "status": "modified"}])

    scope = await persona_classifier.classify(
        session={"feedback_cycle_count": 1, "persona_scope": prior_scope},
        card=_card(),
        conducting_backend=_RaisingBackend(RuntimeError("boom")),
        config=_Cfg(),
        github_service=gh,
        classifier_model="test/model",
    )
    assert scope is not None
    # Personas + head_sha + classifier_model preserved from prior cycle (carry-forward).
    assert scope["personas"] == prior_scope["personas"]
    assert scope["head_sha"] == prior_scope["head_sha"]
    assert scope["classifier_model"] == prior_scope["classifier_model"]
    # cycle_index and computed_at refreshed so snapshots aren't permanently stale.
    assert scope["cycle_index"] == 1
    assert scope["computed_at"] != prior_scope["computed_at"]


@pytest.mark.asyncio
async def test_warning_rate_limited_via_cooldown(caplog: Any) -> None:
    """T044 — FR-006: two failures in the cooldown window → one warning only."""
    import logging
    gh = _FakeGitHub([{"path": "README.md", "added": 3, "removed": 0, "status": "modified"}])
    cfg = _Cfg()
    cfg.persona_scope.classifier_failure_warning_cooldown_seconds = 600.0
    backend = _RaisingBackend(RuntimeError("boom"))

    with caplog.at_level(logging.WARNING, logger="coordinare.services.persona_classifier"):
        await persona_classifier.classify(
            session={"feedback_cycle_count": 0},
            card=_card(),
            conducting_backend=backend,
            config=cfg,
            github_service=gh,
            classifier_model="test/model",
        )
        await persona_classifier.classify(
            session={"feedback_cycle_count": 1},
            card=_card(),
            conducting_backend=backend,
            config=cfg,
            github_service=gh,
            classifier_model="test/model",
        )

    failed_warnings = [
        r for r in caplog.records
        if r.levelno == logging.WARNING and "persona_scope.classifier.failed" in r.getMessage()
    ]
    assert len(failed_warnings) <= 1


@pytest.mark.asyncio
async def test_forced_full_no_matching_files_is_noop() -> None:
    """No file belongs to the configured class → no override applied."""
    backend = _FakeBackend(_baseline_response())
    gh = _FakeGitHub([
        {"path": "README.md", "added": 3, "removed": 0, "status": "modified"},
    ])
    cfg = _Cfg(forced_full_on_path_classes={"security": ["security_sensitive"]})

    scope = await persona_classifier.classify(
        session={"feedback_cycle_count": 0},
        card=_card(),
        conducting_backend=backend,
        config=cfg,
        github_service=gh,
        classifier_model="test/model",
    )
    assert scope is not None
    security = scope["personas"]["security"]
    assert security["depth"] == "skim"
    assert not any(
        o.startswith("forced_full_on_path_class:")
        for o in security.get("overrides", [])
    )


# ---------------------------------------------------------------------------
# get_pr_files outage + truncation handling
# ---------------------------------------------------------------------------


class _OutageGitHub:
    """Returns the error-shaped dict that github.get_pr_files emits on REST failure."""

    async def get_pr_files(self, owner: str, repo: str, pr_number: int) -> dict[str, Any]:
        return {
            "files": [],
            "head_sha": "",
            "truncated": False,
            "error": "pr_fetch_status:503",
        }


class _RaisingGitHub:
    async def get_pr_files(self, owner: str, repo: str, pr_number: int) -> dict[str, Any]:
        raise RuntimeError("network down")


class _TruncatedGitHub:
    async def get_pr_files(self, owner: str, repo: str, pr_number: int) -> dict[str, Any]:
        return {
            "files": [
                {"path": "README.md", "added": 1, "removed": 0, "status": "modified"},
            ],
            "head_sha": "abc123",
            "truncated": True,
            "error": None,
        }


@pytest.mark.asyncio
async def test_gh_outage_dict_error_falls_back_to_full() -> None:
    """get_pr_files reporting `error` → classifier emits gh_outage fallback."""
    scope = await persona_classifier.classify(
        session={"feedback_cycle_count": 0},
        card=_card(),
        conducting_backend=_FakeBackend(_baseline_response()),
        config=_Cfg(),
        github_service=_OutageGitHub(),
        classifier_model="test/model",
    )
    assert scope is not None
    for name, slice_ in scope["personas"].items():
        if name == "closer":
            assert "closer_is_scope_invariant" in slice_["overrides"]
        else:
            assert slice_["depth"] == "full"
            assert any(o.startswith("classifier_failed:gh_outage:") for o in slice_["overrides"])


@pytest.mark.asyncio
async def test_gh_raised_exception_falls_back_to_full() -> None:
    """get_pr_files raising → classifier treats as outage, not silent empty."""
    scope = await persona_classifier.classify(
        session={"feedback_cycle_count": 0},
        card=_card(),
        conducting_backend=_FakeBackend(_baseline_response()),
        config=_Cfg(),
        github_service=_RaisingGitHub(),
        classifier_model="test/model",
    )
    assert scope is not None
    for name, slice_ in scope["personas"].items():
        if name == "closer":
            continue
        assert slice_["depth"] == "full"
        assert any(o.startswith("classifier_failed:gh_outage:raised:") for o in slice_["overrides"])


@pytest.mark.asyncio
async def test_pr_truncation_forces_full_for_non_skip_personas() -> None:
    """truncated=True → every non-skip persona depth flips to full, override tagged."""
    scope = await persona_classifier.classify(
        session={"feedback_cycle_count": 0},
        card=_card(),
        conducting_backend=_FakeBackend(_baseline_response()),
        config=_Cfg(),
        github_service=_TruncatedGitHub(),
        classifier_model="test/model",
    )
    assert scope is not None
    personas = scope["personas"]
    # tech_writer was "skip" in the baseline — must stay skip
    assert personas["tech_writer"]["depth"] == "skip"
    assert "pr_files_truncated" not in personas["tech_writer"]["overrides"]
    # non-skip personas should be full + tagged
    for name in ("reviewer", "security", "qa", "closer"):
        assert personas[name]["depth"] == "full"
        if personas[name]["depth"] != personas[name].get("_orig_depth", personas[name]["depth"]):
            pass
    for name in ("reviewer", "security", "qa"):
        assert "pr_files_truncated" in personas[name]["overrides"]


# ---------------------------------------------------------------------------
# Malformed pr_url robustness
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_url",
    [
        "",
        "not-a-url",
        "https://github.com/acme/repo",
        "https://github.com/acme/repo/pull/",
        "https://github.com/acme/repo/pull/abc",
        "https://github.com/acme/repo/issues/91",
        "https://github.com/pull/91",
    ],
)
@pytest.mark.asyncio
async def test_malformed_pr_url_returns_none(bad_url: str) -> None:
    """Classifier degrades gracefully when pr_url can't be parsed as a PR ref."""
    card = {"id": "PVI_X", "title": "t", "body": "b", "pr_url": bad_url}
    scope = await persona_classifier.classify(
        session={"feedback_cycle_count": 0},
        card=card,
        conducting_backend=_FakeBackend(_baseline_response()),
        config=_Cfg(),
        github_service=_FakeGitHub([]),
        classifier_model="test/model",
    )
    assert scope is None


@pytest.mark.asyncio
async def test_pr_url_with_query_string_parses() -> None:
    """Query strings and trailing slashes after /pull/N must still parse."""
    card = {
        "id": "PVI_X",
        "title": "t",
        "body": "b",
        "pr_url": "https://github.com/acme/repo/pull/91/?tab=files",
    }
    scope = await persona_classifier.classify(
        session={"feedback_cycle_count": 0},
        card=card,
        conducting_backend=_FakeBackend(_baseline_response()),
        config=_Cfg(),
        github_service=_FakeGitHub([
            {"path": "src/foo.py", "added": 1, "removed": 0, "status": "modified"},
        ]),
        classifier_model="test/model",
    )
    assert scope is not None
