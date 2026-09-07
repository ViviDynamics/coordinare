"""Contract tests: dispatch payload survives coordinare → performer boundary.

These tests verify that every field added to card_context by dispatch_performer
actually arrives in the performer's Score model after passing through
AgentService.dispatch_card() and the wire protocol.

If a test fails here, it means a field is being silently dropped — the most
dangerous class of bug in the coordinare-performer contract.
"""
from __future__ import annotations

from typing import Any, ClassVar

import pytest

from coordinare.protocol import ProtocolResponse
from coordinare.services.agent_service import AgentService


class _CaptureTransport:
    """Transport that captures the serialized payload without sending it."""

    def __init__(self) -> None:
        self.captured_payload: dict[str, Any] = {}

    async def send(self, message: Any) -> ProtocolResponse:
        self.captured_payload = dict(message.payload)
        return ProtocolResponse(status="accepted", session_id="test-session")

    async def close(self) -> None:
        pass


def _full_card_context() -> dict[str, Any]:
    """Build a card_context with ALL fields that dispatch_performer might set."""
    return {
        # Card identity (from check_board)
        "id": "PVTI_test123",
        "title": "Implement breadcrumbs",
        "description": "Add breadcrumb navigation to all pages",
        "acceptance_criteria": ["Breadcrumbs show on every page", "Tests pass"],
        "status": "IN_PROGRESS",
        "previous_status": "TODO",
        "issue_id": "ISS_test123",
        "issue_number": 42,
        "issue_url": "https://github.com/org/repo/issues/42",
        # Performer lifecycle (from dispatch_performer)
        "role": "reviewing",
        "persona_instructions": "Focus on test coverage and code quality.",
        "relay_feedback": [{"body": "Please fix the rubocop violations"}],
        "pr_url": "https://github.com/org/repo/pull/99",
        "pr_node_id": "PR_kwDO123456",
        "pr_diff": "diff --git a/src/app.py b/src/app.py\n+    margin = base * 0.9\n",
        "architecture_plan_path": "docs/coordinare-architecture.md",
        "clarifications": [{"questions": ["What framework?"], "answer": "Rails 7"}],
        # 123 US4: answered Q&A carried into an assessor re-dispatch.
        "prior_clarifications": [{"question": "Which ORM?", "answer": "ActiveRecord"}],
        # Backend selection (037)
        "backend": "claude_code",
        "model": "claude-sonnet-4-20250514",
        # GitHub Enterprise (036)
        "github_api_url": "https://github.example.com/api/v3",
        # 164: role workflow fields -- the "all fields survive" test must cover
        # them too, not only their dedicated tests (round-one finding that never
        # received a verdict; dispositioned by hand).
        "workflow": "qa",
        "workflow_env": {"PORT": "3000", "QA_APP_START_COMMAND": "python app.py"},
        "qa_findings": [{"file": "a.py", "line": 1, "category": "unmet_criterion", "severity": "high"}],
        "implementation_brief": {"summary": "s", "milestones": [{"goal": "g", "scope": ["a"], "done_when": "d"}], "size": "small"},
        "documentation_brief": {"summary": "s", "docs": [{"topic": "t", "location": "wiki/x.md", "say": "y"}]},
        "verification_brief": {"summary": "s", "criteria": [{"surface": "/", "action": "open", "expected": "ok", "kind": "functional"}]},
        "implementer_single_turn": True,
        # 166: assessor workflow assessment (injected into architecting dispatch only)
        "assessment": {"ready": True, "goal": "Add time entry categories", "expected_behavior": "Users select category", "out_of_scope": [], "questions": [], "assumptions": [], "criteria": [{"surface": "/", "action": "open", "expected": "ok", "kind": "functional"}], "criteria_source": "card", "clarifications": []},
    }


class TestDispatchPayloadContract:
    """Verify that AgentService.dispatch_card() passes through ALL card_context fields."""

    @pytest.mark.asyncio
    async def test_all_card_context_fields_survive_agent_service(self) -> None:
        """Every field in card_context must appear in the wire payload."""
        transport = _CaptureTransport()
        service = AgentService(transport)
        card_context = _full_card_context()

        await service.dispatch_card(card_context)

        payload = transport.captured_payload
        for key, value in card_context.items():
            assert key in payload, f"Field {key!r} was dropped by AgentService.dispatch_card()"
            assert payload[key] == value, f"Field {key!r} was modified: {payload[key]!r} != {value!r}"

    @pytest.mark.asyncio
    async def test_workspace_info_overlays_card_context(self) -> None:
        """WorkspaceInfo fields are added on top of card_context."""
        from coordinare.workspace import WorkspaceInfo

        transport = _CaptureTransport()
        service = AgentService(transport)
        card_context = _full_card_context()
        workspace = WorkspaceInfo(
            path=None,
            branch="coordinare/test-branch",
            repo_url="https://github.com/org/repo.git",
            github_token="ghs_test_token",
        )

        await service.dispatch_card(card_context, workspace_info=workspace)

        payload = transport.captured_payload
        # Workspace fields overlay card_context
        assert payload["repo_url"] == "https://github.com/org/repo.git"
        assert payload["branch"] == "coordinare/test-branch"
        assert payload["github_token"] == "ghs_test_token"
        # Original card_context fields still present
        assert payload["role"] == "reviewing"
        assert payload["relay_feedback"] == [{"body": "Please fix the rubocop violations"}]
        assert payload["pr_url"] == "https://github.com/org/repo/pull/99"

    @pytest.mark.asyncio
    async def test_role_field_is_not_dropped(self) -> None:
        """The 'role' field is critical — it determines which performer path runs."""
        transport = _CaptureTransport()
        service = AgentService(transport)

        for role in ["implementing", "reviewing", "security", "qa", "documenting", "architecting", "assessing"]:
            await service.dispatch_card({"role": role, "title": "test", "id": "X"})
            assert transport.captured_payload["role"] == role, f"role={role!r} was dropped"

    @pytest.mark.asyncio
    async def test_workflow_is_not_dropped(self) -> None:
        """164: the workflow name selects a multi-step role workflow inside the
        performer.  Score uses extra="ignore", so an unregistered field is
        dropped in transit — the role would look configured and never run its
        workflow, with no error anywhere.  Registered in the payload contract."""
        transport = _CaptureTransport()
        service = AgentService(transport)

        await service.dispatch_card({"workflow": "qa", "title": "test", "id": "X"})

        assert transport.captured_payload["workflow"] == "qa"

    @pytest.mark.asyncio
    async def test_workflow_env_is_not_dropped(self) -> None:
        """164: the operator's app boot settings for a role workflow. Before
        this, QA_APP_START_COMMAND was an env var nothing in production set."""
        transport = _CaptureTransport()
        service = AgentService(transport)
        env = {"QA_APP_START_COMMAND": "bin/rails s -p 3000", "PORT": "3000"}

        await service.dispatch_card({"workflow_env": env, "title": "test", "id": "X"})

        assert transport.captured_payload["workflow_env"] == env

    @pytest.mark.asyncio
    async def test_qa_findings_are_not_dropped(self) -> None:
        """164: the repair brief from the previous QA round, carried into the
        implementer's dispatch the way scanner_findings already is."""
        transport = _CaptureTransport()
        service = AgentService(transport)
        findings = [
            {
                "file": "app/models/user.rb",
                "line": 42,
                "category": "unexpected_regression",
                "severity": "high",
                "criterion": "Users can sign in",
                "expected": "password field present",
                "observed": "password field absent",
            }
        ]

        await service.dispatch_card(
            {"qa_findings": findings, "title": "test", "id": "X"}
        )

        assert transport.captured_payload["qa_findings"] == findings

    @pytest.mark.asyncio
    async def test_blueprint_briefs_are_not_dropped(self) -> None:
        """165: the three reader-specific projections of the architect's
        blueprint and the single-turn flag reach the performer intact."""
        transport = _CaptureTransport()
        service = AgentService(transport)
        briefs = {
            "implementation_brief": {"summary": "s", "milestones": [{"goal": "g", "scope": ["a"], "done_when": "d"}], "size": "large"},
            "documentation_brief": {"summary": "s", "docs": [{"topic": "t", "location": "wiki/x.md", "say": "y"}]},
            "verification_brief": {"summary": "s", "criteria": [{"surface": "/", "action": "open", "expected": "ok", "kind": "functional"}]},
            "implementer_single_turn": False,
        }
        await service.dispatch_card({**briefs, "title": "test", "id": "X"})
        for key, value in briefs.items():
            assert transport.captured_payload[key] == value, key

    @pytest.mark.asyncio
    async def test_assessment_is_not_dropped(self) -> None:
        """166: the assessor's structured assessment reaches the performer
        intact when injected by dispatch_performer."""
        transport = _CaptureTransport()
        service = AgentService(transport)
        assessment = {
            "ready": True,
            "goal": "Add time entry categories",
            "expected_behavior": "Users select category",
            "out_of_scope": ["Category management UI"],
            "questions": [],
            "assumptions": [],
            "criteria": [{"surface": "/time_entries/new", "action": "open", "expected": "category select", "kind": "functional"}],
            "criteria_source": "card",
            "clarifications": [],
        }
        await service.dispatch_card({"title": "test", "id": "X", "assessment": assessment})
        assert transport.captured_payload["assessment"] == assessment

    async def test_briefs_are_declared_on_score_so_extra_ignore_keeps_them(self) -> None:
        from performer.models import Score

        score = Score(
            card_id="X", title="t", description="d", acceptance_criteria=[],
            repo_url="https://github.com/o/r", branch="b",
            implementation_brief={"size": "small"}, documentation_brief={"docs": []},
            verification_brief={"criteria": []}, implementer_single_turn=True,
        )
        assert score.implementation_brief == {"size": "small"}
        assert score.documentation_brief == {"docs": []}
        assert score.verification_brief == {"criteria": []}
        assert score.implementer_single_turn is True
        bare = Score(card_id="X", title="t", description="d", acceptance_criteria=[], repo_url="https://github.com/o/r", branch="b")
        assert bare.implementation_brief == {} and bare.implementer_single_turn is False

    async def test_disputed_feedback_is_not_dropped(self) -> None:
        """126: disputed_feedback carries dispute-adjudication context for the
        raising stage — must survive the boundary."""
        transport = _CaptureTransport()
        service = AgentService(transport)
        disputes = [{"id": "fb-1", "body": "wrong finding", "reason": "already correct"}]

        await service.dispatch_card(
            {"disputed_feedback": disputes, "title": "test", "id": "X"}
        )

        assert transport.captured_payload["disputed_feedback"] == disputes

    async def test_relay_feedback_is_not_dropped(self) -> None:
        """relay_feedback carries human review comments — must survive the boundary."""
        transport = _CaptureTransport()
        service = AgentService(transport)
        feedback = [
            {"body": "Fix the rubocop violations"},
            {"body": "Add tests for the new helper"},
        ]

        await service.dispatch_card({"relay_feedback": feedback, "title": "test", "id": "X"})

        assert transport.captured_payload["relay_feedback"] == feedback

    @pytest.mark.asyncio
    async def test_pr_url_and_node_id_are_not_dropped(self) -> None:
        """pr_url and pr_node_id are needed by reviewer/security/QA roles."""
        transport = _CaptureTransport()
        service = AgentService(transport)

        await service.dispatch_card({
            "pr_url": "https://github.com/org/repo/pull/99",
            "pr_node_id": "PR_kwDO123",
            "title": "test",
            "id": "X",
        })

        assert transport.captured_payload["pr_url"] == "https://github.com/org/repo/pull/99"
        assert transport.captured_payload["pr_node_id"] == "PR_kwDO123"

    @pytest.mark.asyncio
    async def test_pr_diff_is_not_dropped(self) -> None:
        """pr_diff (injected for review roles) must reach the performer wire."""
        transport = _CaptureTransport()
        service = AgentService(transport)

        diff = "diff --git a/src/app.py b/src/app.py\n+    margin = base * 0.9\n"
        await service.dispatch_card({
            "pr_url": "https://github.com/org/repo/pull/99",
            "pr_diff": diff,
            "title": "test",
            "id": "X",
        })

        assert transport.captured_payload["pr_diff"] == diff

    @pytest.mark.asyncio
    async def test_backend_and_model_are_not_dropped(self) -> None:
        """Per-role backend/model selection (037) must survive."""
        transport = _CaptureTransport()
        service = AgentService(transport)

        await service.dispatch_card({
            "backend": "claude_code",
            "model": "claude-sonnet-4-20250514",
            "title": "test",
            "id": "X",
        })

        assert transport.captured_payload["backend"] == "claude_code"
        assert transport.captured_payload["model"] == "claude-sonnet-4-20250514"

    @pytest.mark.asyncio
    async def test_github_api_url_is_not_dropped(self) -> None:
        """GitHub Enterprise URL (036) must survive."""
        transport = _CaptureTransport()
        service = AgentService(transport)

        await service.dispatch_card({
            "github_api_url": "https://github.example.com/api/v3",
            "title": "test",
            "id": "X",
        })

        assert transport.captured_payload["github_api_url"] == "https://github.example.com/api/v3"


_has_performer = True
try:
    import performer.models  # noqa: F401
except ModuleNotFoundError:
    _has_performer = False


@pytest.mark.skipif(not _has_performer, reason="performer package not on PYTHONPATH")
class TestScoreModelContract:
    """Verify that the performer's Score model accepts all dispatch payload fields."""

    def test_score_accepts_all_dispatch_fields(self) -> None:
        """Score must not drop any field from a full dispatch payload."""
        from performer.models import Score

        payload = _full_card_context()
        # Add workspace fields (overlaid by AgentService)
        payload["repo_url"] = "https://github.com/org/repo.git"
        payload["branch"] = "coordinare/test-branch"
        payload["github_token"] = "ghs_test"

        score = Score(**payload)

        assert score.title == "Implement breadcrumbs"
        assert score.role == "reviewing"
        assert score.persona_instructions == "Focus on test coverage and code quality."
        assert score.relay_feedback == [{"body": "Please fix the rubocop violations"}]
        assert score.pr_url == "https://github.com/org/repo/pull/99"
        assert score.pr_node_id == "PR_kwDO123456"
        assert score.pr_diff == (
            "diff --git a/src/app.py b/src/app.py\n+    margin = base * 0.9\n"
        )
        assert score.backend == "claude_code"
        assert score.model == "claude-sonnet-4-20250514"
        assert score.github_api_url == "https://github.example.com/api/v3"
        assert score.architecture_plan_path == "docs/coordinare-architecture.md"
        assert score.assessment is not None
        assert score.assessment["ready"] is True
        assert score.assessment["goal"] == "Add time entry categories"
        assert score.prior_clarifications == [
            {"question": "Which ORM?", "answer": "ActiveRecord"}
        ]

    def test_score_defaults_for_minimal_payload(self) -> None:
        """Score with only required fields should have safe defaults."""
        from performer.models import Score

        score = Score(
            title="Test",
            repo_url="https://github.com/org/repo",
            branch="main",
        )

        assert score.role == "implementing"
        assert score.persona_instructions == ""
        assert score.relay_feedback == []
        assert score.prior_clarifications == []
        assert score.pr_url == ""
        assert score.pr_node_id == ""
        assert score.pr_diff == ""
        assert score.backend == ""
        assert score.model == ""
        assert score.github_api_url == ""


@pytest.mark.skipif(not _has_performer, reason="performer package not on PYTHONPATH")
class TestCardContextKeysAreDeclaredOnScore:
    """Source scan: every key dispatch_performer writes into ``card_context``
    must be declared on the performer's ``Score`` model.

    ``Score`` sets ``extra="ignore"``, so an injected-but-undeclared field is
    dropped silently at the coordinare -> performer boundary: the coordinare logs
    a correct dispatch, the performer never sees the value, and nothing fails.
    The per-field tests above only catch this for fields somebody remembered to
    write a test for. This scan catches the whole class.
    """

    # Keys that are deliberately NOT Score fields. Each entry needs a reason;
    # an unexplained entry here is the bug this test exists to prevent.
    _INTENTIONALLY_NOT_ON_SCORE: ClassVar[set[str]] = {
        # 092: secret-like, so http_performer_service routes it into the
        # redacted `secrets` channel and strips it from the metadata that
        # becomes Score (http_performer_service.py: `k != "test_env_vars"`).
        "test_env_vars",
    }

    # Keys dispatch_performer injects that Score drops today. These are real
    # gaps, not exemptions: no performer code reads any of them, so the value
    # never reaches a backend prompt. Declaring the field is necessary but not
    # sufficient -- each also needs a consumer -- so they are tracked here
    # rather than papered over. Shrink this set; never grow it.
    _KNOWN_DROPPED: ClassVar[set[str]] = {
        "max_tool_calls",     # persona-slice scope tier cap
        "repair_mandate",     # 090-L3 baseline-repair mandate
        "scanner_findings",   # 083 security advisory ceiling
        "scope_addon",        # persona-slice prompt addon
        "scope_focus",        # persona-slice focus
    }

    @staticmethod
    def _injected_keys() -> set[str]:
        import re
        from pathlib import Path

        import coordinare.graph.nodes.dispatch_performer as dp

        source = Path(dp.__file__).read_text()
        return set(re.findall(r'card_context\[\s*"([^"]+)"\s*\]\s*=', source))

    def test_scan_finds_the_known_injection_sites(self) -> None:
        """Guard the regex itself: if it stops matching, the scan below passes
        vacuously and the whole class of bug goes unnoticed again."""
        keys = self._injected_keys()
        assert len(keys) >= 20, f"regex matched only {len(keys)} keys -- scan is broken"
        for expected in ("role", "persona_instructions", "prior_clarifications"):
            assert expected in keys, f"{expected!r} not found by the scan"

    def test_every_injected_key_is_declared_on_score(self) -> None:
        """No NEW field may be injected without being declared on Score."""
        from performer.models import Score

        declared = set(Score.model_fields)
        undeclared = (
            self._injected_keys()
            - declared
            - self._INTENTIONALLY_NOT_ON_SCORE
            - self._KNOWN_DROPPED
        )
        assert not undeclared, (
            "dispatch_performer injects these into card_context but Score does "
            f"not declare them, so extra=\"ignore\" drops them in transit: "
            f"{sorted(undeclared)}. Declare each on performer.models.Score and "
            "add a row to specs/contracts/dispatch-payload.md."
        )

    def test_known_dropped_set_is_accurate(self) -> None:
        """Keep the gap register honest: an entry that is now declared (or no
        longer injected) must be removed, or the register rots into a
        permanent excuse."""
        from performer.models import Score

        declared = set(Score.model_fields)
        injected = self._injected_keys()
        for key in self._KNOWN_DROPPED:
            assert key in injected, f"{key!r} is no longer injected -- drop it from _KNOWN_DROPPED"
            assert key not in declared, f"{key!r} is now declared on Score -- drop it from _KNOWN_DROPPED"


@pytest.mark.skipif(not _has_performer, reason="performer package not on PYTHONPATH")
class TestPriorClarificationsContract:
    """123 US4 (FR-011): answered assessor Q&A carried forward on re-dispatch."""

    @pytest.mark.asyncio
    async def test_prior_clarifications_is_not_dropped_by_agent_service(self) -> None:
        transport = _CaptureTransport()
        service = AgentService(transport)
        prior = [{"question": "What framework?", "answer": "Rails 7"}]

        await service.dispatch_card(
            {"prior_clarifications": prior, "title": "test", "id": "X"}
        )

        assert transport.captured_payload["prior_clarifications"] == prior

    def test_prior_clarifications_survives_score_validation(self) -> None:
        """The field must be declared, or extra="ignore" drops it before the
        spec-166 assessor intake can merge it."""
        from performer.models import Score

        prior = [{"question": "What framework?", "answer": "Rails 7"}]
        score = Score(
            title="t",
            repo_url="https://github.com/o/r",
            branch="b",
            role="assessing",
            prior_clarifications=prior,
        )

        assert score.prior_clarifications == prior

    def test_prior_clarifications_defaults_to_empty_list(self) -> None:
        from performer.models import Score

        score = Score(title="t", repo_url="https://github.com/o/r", branch="b")

        assert score.prior_clarifications == []

    def test_prior_clarifications_reaches_the_assessor_intake(self) -> None:
        """End of the chain: what dispatch_performer injects is what the
        assessor's intake actually merges (spec 166 reads it off the Score)."""
        from performer.models import Score
        from performer.workflows.assessor.intake import build_intake

        score = Score(
            title="t",
            repo_url="https://github.com/o/r",
            branch="b",
            role="assessing",
            prior_clarifications=[
                {"question": "What framework?", "answer": "Rails 7"}
            ],
        )

        intake = build_intake(score)

        assert any(
            c["question"] == "What framework?" and c["answer"] == "Rails 7"
            for c in intake.clarifications
        ), f"prior Q&A missing from intake: {intake.clarifications}"
        assert intake.answered_rounds == 1
