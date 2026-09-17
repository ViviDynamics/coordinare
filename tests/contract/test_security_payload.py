"""Contract tests: security workflow dispatch payload (spec 170)."""
from __future__ import annotations

from typing import Any

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


def _full_security_card_context() -> dict[str, Any]:
    """Build a card_context for security dispatch with all 170-related fields."""
    return {
        # Card identity (from check_board)
        "id": "PVTI_test123",
        "title": "Fix SQL injection vulnerability",
        "description": "User input is concatenated into SQL queries",
        "acceptance_criteria": ["No SQL injection vulnerabilities", "Tests pass"],
        "status": "IN_PROGRESS",
        # Performer lifecycle (from dispatch_performer)
        "role": "security",
        "persona_instructions": "Scan for security vulnerabilities and verify findings.",
        "pr_url": "https://github.com/org/repo/pull/99",
        "pr_node_id": "PR_kwDO123456",
        # 170: pr_diff injected for security when workflow is set
        "pr_diff": "diff --git a/app.py b/app.py\n--- a/app.py\n+++ b/app.py\n@@ -1,3 +1,3 @@\n-query = 'SELECT * FROM users WHERE id=' + request.args['id']\n+query = 'SELECT * FROM users WHERE id = %s'\n",
        # 170: scanner_findings from 083 floor (empty when workflow runs)
        "scanner_findings": [],
        # 164: role workflow fields
        "workflow": "security",
        "workflow_env": {"SECURITY_SCAN_TIMEOUT_S": "120"},
        # Backend selection (037)
        "backend": "claude_code",
        "model": "claude-opus-4-1-20250805",
        # GitHub Enterprise (036)
        "github_api_url": "https://github.example.com/api/v3",
    }


def _prose_security_card_context() -> dict[str, Any]:
    """Build a card_context for prose security dispatch (without workflow)."""
    return {
        # Card identity
        "id": "PVTI_test456",
        "title": "Review security",
        "description": "Prose security review",
        "status": "IN_PROGRESS",
        # Performer lifecycle
        "role": "security",
        "persona_instructions": "Review for security issues.",
        "pr_url": "https://github.com/org/repo/pull/100",
        "pr_node_id": "PR_kwDO123457",
        # 412: no scanner_findings here -- the floor findings (below) relay
        # verbatim on the prose path.
        # No pr_diff for prose security (083 fetches it itself)
        # No workflow for prose path
        # 412 round 27: the prose path relays the floor findings verbatim
        # (the workflow path is where they are emptied) -- keep a non-empty
        # finding here so the relay preservation is actually verified.
        "scanner_findings": [
            {"severity": "high", "path": "app.py", "line": 3, "category": "sql_injection", "message": "tainted query"},
        ],
        # Backend selection
        "backend": "claude_code",
        "model": "claude-opus-4-1-20250805",
    }


class TestSecurityWorkflowPayload:
    """Verify security workflow payload fields survive the dispatch boundary."""

    @pytest.mark.asyncio
    async def test_security_workflow_fields_survive(self) -> None:
        """When security role runs workflow, pr_diff and empty scanner_findings survive."""
        transport = _CaptureTransport()
        service = AgentService(transport)
        card_context = _full_security_card_context()

        await service.dispatch_card(card_context)

        payload = transport.captured_payload
        # pr_diff should be present when workflow is set
        assert "pr_diff" in payload, "pr_diff was dropped for security workflow dispatch"
        assert payload["pr_diff"] == card_context["pr_diff"]

        # scanner_findings should be empty list when workflow runs
        assert "scanner_findings" in payload, "scanner_findings was dropped"
        assert payload["scanner_findings"] == []

        # workflow should be present
        assert "workflow" in payload
        assert payload["workflow"] == "security"


    @pytest.mark.asyncio
    async def test_prose_security_scanner_findings_survive(self) -> None:
        """412: the coordinare floor is gone on the workflow path, but the
        prose path relays its floor findings verbatim through dispatch."""
        transport = _CaptureTransport()
        service = AgentService(transport)
        card_context = _prose_security_card_context()

        await service.dispatch_card(card_context)

        payload = transport.captured_payload
        # 412 round 27: a non-empty floor finding must survive the dispatch
        # boundary -- an empty-list assertion here would be vacuous.
        assert "scanner_findings" in payload, "scanner_findings was dropped"
        assert payload["scanner_findings"] == card_context["scanner_findings"]
        assert payload["scanner_findings"], "fixture must carry a non-empty finding"

        # pr_diff should not be present for prose security
        assert "pr_diff" not in payload


    @pytest.mark.asyncio
    async def test_review_findings_with_security_source(self) -> None:
        """When review_findings are lifted from security workflow, they survive dispatch."""
        transport = _CaptureTransport()
        service = AgentService(transport)
        card_context = {
            "id": "PVTI_test789",
            "title": "Fix security findings",
            "description": "Implement security fixes",
            "status": "IN_PROGRESS",
            "role": "implementing",
            "persona_instructions": "Fix the security findings.",
            # 170: review_findings lifted from security workflow to implementing dispatch
            "review_findings": {
                "changed_files": [{"path": "app.py", "lines": [40, 41, 42]}],
                "diff_truncated": False,
                "verdict": "changes_requested",
                "covered_files": ["app.py"],
                "findings": [
                    {
                        "path": "app.py",
                        "line": 42,
                        "category": "injection",
                        "problem": "SQL injection vulnerability",
                        "why_blocking": "User input directly concatenated into SQL",
                        "evidence": "query = 'SELECT * FROM users WHERE id=' + request.args['id']",
                        "origin": "model",
                    },
                ],
            },
            "backend": "claude_code",
            "model": "claude-opus-4-1-20250805",
        }

        await service.dispatch_card(card_context)

        payload = transport.captured_payload
        # review_findings should survive intact
        assert "review_findings" in payload, "review_findings was dropped"
        assert payload["review_findings"]["verdict"] == "changes_requested"
        assert len(payload["review_findings"]["findings"]) == 1
        assert payload["review_findings"]["findings"][0]["category"] == "injection"
        assert payload["review_findings"]["findings"][0]["origin"] == "model"
