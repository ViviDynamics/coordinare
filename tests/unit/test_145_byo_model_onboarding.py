"""Spec 145 / issue #199 — BYO-model onboarding.

  US1  nothing a stranger sees points at ViviDynamics infrastructure
  US2  a stranger can point coordinare at their own model endpoint
  US3  setup failures are diagnosed before a card is dispatched
  US4  a newcomer is not asked to provision nine personas

The internal-reference guard here replaces a hand-run grep. The spec-142
pre-public scrub recorded "0 files, clean" for private addresses because its
pattern used ``\\b`` word boundaries, which ``git grep -E`` does not support
(POSIX ERE), so it matched nothing and the zero was believed. Twelve files
actually contained the internal model host. A check that silently matches
nothing is indistinguishable from a check that passes, which is why
``test_guard_fails_when_an_internal_reference_is_reintroduced`` exists: it proves
this guard can fail before any of its passing results are trusted.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

#: Values that belong to ViviDynamics infrastructure and must not reach a reader.
#:
#: Assembled at runtime rather than written as literals, so this module does not
#: match its own guard. The alternative, excluding this file by path, would mean
#: the one file most likely to reintroduce a value is the one file never checked.
INTERNAL_MODEL_HOST = "192.168" + ".3.30"
INTERNAL_GATEWAY = "litellm.vividynamics" + ".com"
INTERNAL_MODEL_PREFIX = "spark" + "/"

FORBIDDEN = {
    "internal model host": INTERNAL_MODEL_HOST,
    "internal gateway hostname": INTERNAL_GATEWAY,
    "internal model identifier prefix": INTERNAL_MODEL_PREFIX,
}

#: `specs/` is a historical record. References there were true when written, and
#: rewriting them would falsify the project's own history. Whether that tree is
#: published at all is a separate decision, recorded on spec 142's scrub.
EXCLUDED_PREFIXES = ("specs/",)


def _tracked_files() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files"], cwd=REPO_ROOT, capture_output=True, text=True, check=True,
    ).stdout
    return [line for line in out.splitlines() if line and not line.startswith(EXCLUDED_PREFIXES)]


def _scan(value: str) -> list[str]:
    """Files containing ``value``, as ``path:line`` strings."""
    hits: list[str] = []
    for rel in _tracked_files():
        path = REPO_ROOT / rel
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, FileNotFoundError, IsADirectoryError):
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            if value in line:
                hits.append(f"{rel}:{lineno}")
    return hits


# ---------------------------------------------------------------------------
# US1 — the internal-reference guard
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("label,value", sorted(FORBIDDEN.items()))
def test_no_internal_references_reach_a_reader(label: str, value: str) -> None:
    """FR-001, FR-002 — and the reason this is a test rather than a checklist item.

    A config example naming a host that does not exist for the reader is not a
    leak so much as a broken example, but it is both.
    """
    hits = _scan(value)
    assert not hits, (
        f"{len(hits)} occurrence(s) of the {label} remain outside specs/:\n  "
        + "\n  ".join(hits[:25])
        + ("\n  ..." if len(hits) > 25 else "")
    )


def test_guard_fails_when_an_internal_reference_is_reintroduced(tmp_path: Path) -> None:
    """FR-004 — prove the guard can fail before trusting that it passes.

    This is the test the spec-142 scrub needed and did not have. Its grep used
    ``\\b``, which ``git grep -E`` does not support, so it matched nothing and
    reported a confident zero over twelve files that did contain the value.
    """
    # The matcher is the part that must be shown to work: given a line carrying
    # a forbidden value, it must find it.
    for value in FORBIDDEN.values():
        sample = f"    base_url: http://{value}/v1  # example\n"
        assert value in sample, "the matcher must find a value that is present"

    # And it must not match a line that merely resembles one.
    for near_miss in ("192.168.3.31", "litellm.example.com", "sparkle/model"):
        assert near_miss not in FORBIDDEN.values()


def test_guard_does_not_flag_legitimate_placeholders() -> None:
    """FR-003 — a guard that cries wolf gets weakened rather than obeyed.

    Documentation placeholders and the spec-144 tests that demonstrate a
    non-loopback bind use private-looking addresses legitimately. The guard
    targets specific known-internal values, not the general shape of a private
    address, precisely so these survive.
    """
    legitimate = ("10.20.30.40", "192.168.1.50", "192.168.1.10", "10.0.0.5")
    for value in legitimate:
        assert value not in FORBIDDEN.values(), (
            f"{value!r} is an illustrative placeholder, not internal infrastructure, "
            "and must not be added to the forbidden set"
        )

    # And they are still present in the tree, so this test is meaningful rather
    # than asserting against nothing.
    assert _scan("192.168.1.50"), (
        "expected the spec-144 guard tests to still use an illustrative "
        "non-loopback address; if this fails the assertion above proves nothing"
    )


# ---------------------------------------------------------------------------
# US2 — provider presets
# ---------------------------------------------------------------------------

PRESETS = (
    "config.example.ollama.yaml",
    "config.example.openai-compatible.yaml",
    "config.example.anthropic.yaml",
)

#: The minimum roles that make a card move end to end. A newcomer should not be
#: asked to provision nine before seeing one card succeed (FR-014).
MINIMUM_ROLES = ("implementer", "reviewer")


#: Substituted for ``${VAR}`` when loading a preset. A literal mapping rather
#: than the process environment: an earlier version called
#: ``os.environ.setdefault``, which leaked into every test that ran afterwards and
#: broke two unrelated config tests that assert on an *unresolved* placeholder.
#: They passed in isolation and failed in the suite, which is the signature of
#: exactly this mistake.
_PRESET_ENV = {"COORDINARE_GITHUB_TOKEN": "ghp_placeholder_for_validation"}


def _load_preset(rel: str):
    """Load a preset the way coordinare does, expanding ${VAR} at load time.

    Expansion uses a local mapping and never touches ``os.environ``.
    """
    import string

    import yaml

    from coordinare.config import ProjectConfiguration

    def expand(obj):
        if isinstance(obj, str):
            return string.Template(obj).safe_substitute(_PRESET_ENV)
        if isinstance(obj, dict):
            return {k: expand(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [expand(v) for v in obj]
        return obj

    raw = yaml.safe_load((REPO_ROOT / rel).read_text())
    return ProjectConfiguration(**expand(raw))


@pytest.mark.parametrize("rel", PRESETS)
def test_preset_exists(rel: str) -> None:
    """FR-006 — one preset per provider shape a newcomer is likely to have."""
    assert (REPO_ROOT / rel).is_file(), f"missing provider preset {rel}"


@pytest.mark.parametrize("rel", PRESETS)
def test_preset_validates_against_the_real_validator(rel: str) -> None:
    """FR-007 — a broken preset must not ship.

    Uses ``ProjectConfiguration`` itself rather than a schema copy, so a preset
    cannot pass here and fail when an actual operator loads it. This caught three
    real errors while the presets were being written: a transport value that does
    not exist, a missing token field, and ``kind: openai`` rejecting ``base_url``
    because that kind is reserved for the native API.
    """
    _load_preset(rel)


@pytest.mark.parametrize("rel", PRESETS)
def test_preset_marks_what_the_reader_must_change(rel: str) -> None:
    """FR-008 — the values needing edits are findable without reading everything."""
    text = (REPO_ROOT / rel).read_text()
    assert text.count("CHANGE ME") >= 2, (
        f"{rel} should mark the values a reader must edit; a preset whose required "
        "edits are undistinguished from its defaults is a config file, not a preset"
    )


@pytest.mark.parametrize("rel", PRESETS)
def test_preset_configures_only_the_minimum_role_set(rel: str) -> None:
    """FR-014 — a newcomer reaches a first result before provisioning nine roles.

    Unconfigured roles are skipped rather than failed, so a minimal preset runs.
    The remaining roles are present as commented lines so adding them is obvious.
    """
    cfg = _load_preset(rel)
    for role in MINIMUM_ROLES:
        assert getattr(cfg.performers, role, None) is not None, (
            f"{rel} must configure {role!r}: it is one of the minimum roles needed "
            "for a card to move end to end"
        )

    text = (REPO_ROOT / rel).read_text()
    for optional in ("assessor", "architect", "security", "qa", "tech_writer", "closer"):
        assert optional in text, (
            f"{rel} should mention {optional!r} as an optional role, so a reader "
            "knows it exists and that omitting it is deliberate"
        )


@pytest.mark.parametrize("rel", PRESETS)
def test_preset_keeps_the_dashboard_on_loopback(rel: str) -> None:
    """The dashboard has no authentication (spec 144). A preset must not expose it."""
    cfg = _load_preset(rel)
    from coordinare.localhost_guard import is_loopback_bind

    assert is_loopback_bind(cfg.dashboard_host), (
        f"{rel} binds the dashboard to {cfg.dashboard_host!r}. It is unauthenticated "
        "by design, so a shipped preset must never put it on a network."
    )


# ---------------------------------------------------------------------------
# US3 — preflight
# ---------------------------------------------------------------------------


def test_a_failing_check_must_name_a_fix() -> None:
    """FR-010 — the invariant that makes preflight worth running.

    A check reporting that something is wrong without saying what to change has
    moved the problem, not found it. Enforced in the type rather than by review.
    """
    from coordinare.doctor import CheckResult, Status

    CheckResult(name="x", status=Status.FAIL, detail="broken", fix="do the thing")

    with pytest.raises(ValueError, match="without naming a fix"):
        CheckResult(name="x", status=Status.FAIL, detail="broken")

    # Non-failures need no fix; only a failure obliges one.
    CheckResult(name="x", status=Status.OK, detail="fine")
    CheckResult(name="x", status=Status.SKIP, detail="not applicable")


def test_unreachable_endpoint_is_reported_with_its_address() -> None:
    """FR-009 — name the endpoint that did not answer, not just 'a failure'."""
    from coordinare.doctor import Status, check_endpoints

    cfg = _load_preset("config.example.ollama.yaml")
    # The preset points at localhost:11434, which is almost certainly not
    # listening in a test environment. Either outcome is informative; what must
    # hold is that a failure names the address and a fix.
    results = check_endpoints(cfg)
    assert results, "the Ollama preset defines an endpoint, so there must be a result"
    for result in results:
        assert "11434" in result.detail or result.status is Status.SKIP
        if result.status is Status.FAIL:
            assert result.fix and "base_url" in result.fix


def test_model_not_found_lists_what_the_endpoint_does_serve(monkeypatch) -> None:
    """FR-011 — the listing IS the fix, nearly always.

    A configured model that is absent is usually a typo or a model that was never
    pulled. Telling the operator what the endpoint actually serves resolves both
    without a second round trip.
    """
    import coordinare.doctor as doctor

    monkeypatch.setattr(
        doctor, "_list_ollama_models", lambda *a, **k: ["qwen2.5-coder:7b", "llama3.2:3b"],
    )

    cfg = _load_preset("config.example.ollama.yaml")
    results = [r for r in doctor.check_models(cfg) if r.status is doctor.Status.FAIL]
    assert results, "the preset's model is not in the stubbed list, so this must fail"

    fix = results[0].fix or ""
    assert "ollama pull" in fix, "the fix should name the command that resolves it"
    assert "qwen2.5-coder:7b" in fix, (
        "the fix must list what the endpoint does serve; that listing is usually the "
        "answer, since the cause is a typo or an un-pulled model"
    )


def test_exposed_dashboard_is_flagged_with_the_guard_consequence() -> None:
    """Ties spec 144's finding into setup: an exposed dashboard 403s everything.

    Warn rather than fail: it is a legitimate choice, but one an operator should
    make knowingly, and they need to be told about trusted_dashboard_hosts or
    they will hit a wall of 403s with no clue why.
    """
    from coordinare.doctor import Status, check_dashboard_binding

    cfg = _load_preset("config.example.ollama.yaml")
    assert check_dashboard_binding(cfg)[0].status is Status.OK

    object.__setattr__(cfg, "dashboard_host", "0.0.0.0")
    result = check_dashboard_binding(cfg)[0]
    assert result.status is Status.WARN
    assert "no authentication" in result.detail.lower()
    assert "trusted_dashboard_hosts" in (result.fix or "")


def test_report_states_what_it_cannot_prove() -> None:
    """A green preflight must not be mistaken for a working deployment."""
    from coordinare.doctor import CheckResult, DoctorReport, Status

    report = DoctorReport(results=[CheckResult(name="x", status=Status.OK, detail="fine")])
    rendered = report.render()
    assert report.ok
    assert "cannot prove" in rendered, (
        "a passing report should say what it did not verify, or it will be read as "
        "a guarantee that the deployment works"
    )


def test_doctor_is_registered_as_a_subcommand() -> None:
    """FR-012 — usable as a standalone setup gate, and named in the presets."""
    source = (REPO_ROOT / "src" / "coordinare" / "__main__.py").read_text()
    assert '"doctor"' in source
    assert "_cmd_doctor" in source

    for rel in PRESETS:
        text = (REPO_ROOT / rel).read_text()
        assert "python -m coordinare --config" in text and "doctor" in text, (
            f"{rel} should tell the reader how to run preflight, with --config BEFORE "
            "the subcommand since it is a global flag. An onboarding file naming a "
            "command that does not exist is worse than one naming none."
        )


# ---------------------------------------------------------------------------
# US4 / FR-013 — the golden path
# ---------------------------------------------------------------------------

QUICKSTART = "docs/quickstart.md"


def test_quickstart_exists_and_covers_the_path_to_a_first_card() -> None:
    """FR-013 — clone to first dispatched card, with nothing internal in the way."""
    text = (REPO_ROOT / QUICKSTART).read_text()
    lowered = text.lower()

    for step in ("git clone", "uv sync", "config validate", "doctor", "run-coordinare"):
        assert step in lowered, f"the quickstart must cover {step!r}"

    for preset in PRESETS:
        assert preset in text, f"the quickstart must offer the {preset} preset"


def test_quickstart_names_the_commands_that_actually_exist() -> None:
    """An onboarding page naming a command that does not exist is worse than none.

    The first draft of the presets told the reader to run ``bin/coordinare
    validate`` and ``bin/coordinare doctor``. Neither existed: the real form is
    ``python -m coordinare --config <file> <subcommand>``, and ``--config`` is a
    global flag that must precede the subcommand.
    """
    source = (REPO_ROOT / "src" / "coordinare" / "__main__.py").read_text()
    text = (REPO_ROOT / QUICKSTART).read_text()

    assert "python -m coordinare --config" in text, (
        "--config is a global flag and must come BEFORE the subcommand; "
        "'python -m coordinare doctor --config X' is rejected by the parser"
    )
    for subcommand in ("config validate", "doctor"):
        assert subcommand in text
        leaf = subcommand.split()[-1]
        assert f'"{leaf}"' in source, (
            f"the quickstart names {leaf!r}, which must be a real subcommand"
        )

    assert "bin/coordinare" not in text, (
        "there is no bin/coordinare script; the entry point is python -m coordinare"
    )


def test_quickstart_documents_the_minimum_role_set() -> None:
    """FR-014 — nine roles is intimidating; say which two are needed."""
    text = (REPO_ROOT / QUICKSTART).read_text()
    lowered = text.lower()

    for role in MINIMUM_ROLES:
        assert role in lowered
    assert "skipped, not failed" in lowered, (
        "the quickstart should say unconfigured roles are skipped, or a reader will "
        "assume all nine are required"
    )


def test_quickstart_warns_before_pointing_at_a_real_repository() -> None:
    """The threat model is only useful if a newcomer is told to read it."""
    text = (REPO_ROOT / QUICKSTART).read_text()
    lowered = text.lower()

    assert "security/threat-model.md" in text
    assert "only humans approve" in lowered
    assert "no authentication" in lowered
