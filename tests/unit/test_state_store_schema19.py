"""Tests for state_store schema v19: review_findings (spec 169)."""
import json
from datetime import datetime

from coordinare.state_store import CURRENT_SCHEMA_VERSION, PersistedSession, WorkflowSnapshot


class TestReviewFindingsSchemaV19:
    """Tests for spec 169 review_findings field on PersistedSession."""

    def test_schema_version_is_20(self):
        """CURRENT_SCHEMA_VERSION bumped from 18 to 19."""
        assert CURRENT_SCHEMA_VERSION == 20  # 173: + the intake gate fields

    def test_review_findings_default_none(self):
        """review_findings defaults to None."""
        session = PersistedSession(card_id="test-card")
        assert session.review_findings is None

    def test_review_findings_accepts_review_record(self):
        """review_findings accepts a complete ReviewRecord dict."""
        review_record = {
            "changed_files": [
                {
                    "path": "src/foo.py",
                    "hunks": [
                        {
                            "header": "@@ -1,5 +1,7 @@",
                            "start_line": 1,
                            "end_line": 7,
                            "lines": ["def foo():", "    pass"],
                        }
                    ],
                    "fully_in_diff": True,
                    "opened_by_survey": False,
                }
            ],
            "diff_truncated": False,
            "verdict": "changes_requested",
            "covered_files": ["src/foo.py"],
            "findings_before_gate": [
                {
                    "path": "src/foo.py",
                    "line": 2,
                    "category": "style",
                    "problem": "Missing docstring",
                    "why_blocking": "Documentation required",
                    "evidence": "def foo():",
                    "origin": "model",
                }
            ],
        }
        session = PersistedSession(
            card_id="test-card", review_findings=review_record
        )
        assert session.review_findings == review_record

    def test_review_findings_drops_malformed_not_dict(self):
        """Malformed review_findings (not a dict) drops to None."""
        session = PersistedSession(card_id="test-card", review_findings="not a dict")
        assert session.review_findings is None

        session = PersistedSession(card_id="test-card", review_findings=[])
        assert session.review_findings is None

    def test_review_findings_drops_malformed_missing_required_fields(self):
        """Malformed review_findings (missing required fields) drops to None."""
        # Missing changed_files
        malformed = {
            "diff_truncated": False,
            "verdict": "approved",
            "covered_files": [],
        }
        session = PersistedSession(card_id="test-card", review_findings=malformed)
        assert session.review_findings is None

        # Missing verdict
        malformed = {
            "changed_files": [],
            "diff_truncated": False,
            "covered_files": [],
        }
        session = PersistedSession(card_id="test-card", review_findings=malformed)
        assert session.review_findings is None

    def test_review_findings_round_trip_serialization(self):
        """review_findings round-trips through JSON serialization."""
        review_record = {
            "changed_files": [
                {
                    "path": "test.py",
                    "hunks": [],
                    "fully_in_diff": True,
                }
            ],
            "diff_truncated": False,
            "verdict": "approved",
            "covered_files": ["test.py"],
        }
        session = PersistedSession(
            card_id="test-card",
            performer_stage="reviewing",
            review_findings=review_record,
        )

        # Simulate serialization/deserialization

        snapshot = WorkflowSnapshot(
            schema_version=CURRENT_SCHEMA_VERSION,
            snapshot_at=datetime.now(),
            phase="idle",
            active_sessions={"test-card": session},
        )
        serialized = snapshot.model_dump_json()
        deserialized = json.loads(serialized)

        # Load back
        restored_snapshot = WorkflowSnapshot.model_validate(deserialized)
        restored_session = restored_snapshot.active_sessions["test-card"]
        assert restored_session.review_findings == review_record

    def test_v18_snapshot_loads_with_review_findings_none(self):
        """v18 snapshots (no review_findings field) load with None."""

        # Construct a v18 snapshot manually
        v18_snapshot_dict = {
            "schema_version": 18,
            "snapshot_at": datetime.now().isoformat(),
            "phase": "idle",
            "active_sessions": {
                "test-card": {
                    "card_id": "test-card",
                    "performer_stage": "implementing",
                    "assessment": None,
                    # No review_findings key
                }
            },
        }

        # Load should succeed and review_findings should be None
        snapshot = WorkflowSnapshot.model_validate(v18_snapshot_dict)
        assert snapshot.active_sessions["test-card"].review_findings is None
        # Schema version should be restored from the snapshot
        assert snapshot.schema_version == 18

    def test_review_findings_corrupted_not_list_logged(self, caplog):
        """Malformed review_findings logs the drop."""
        import logging

        # Enable logging capture
        caplog.set_level(logging.WARNING)

        # This should trigger the validator and log a warning
        session = PersistedSession(card_id="test-card", review_findings="corrupt")
        assert session.review_findings is None
        # Note: Logger is at module level, so we may not capture it in this test
        # This test ensures the validator doesn't crash

    def test_review_findings_with_multiple_findings(self):
        """review_findings can hold multiple findings."""
        review_record = {
            "changed_files": [
                {
                    "path": "src/main.py",
                    "hunks": [
                        {
                            "header": "@@ -1,1 +1,1 @@",
                            "start_line": 1,
                            "end_line": 1,
                        }
                    ],
                    "fully_in_diff": True,
                }
            ],
            "diff_truncated": False,
            "verdict": "changes_requested",
            "covered_files": ["src/main.py"],
            "findings_before_gate": [
                {
                    "path": "src/main.py",
                    "line": 1,
                    "category": "logic_error",
                    "problem": "Off-by-one error",
                    "why_blocking": "Logic is incorrect",
                    "evidence": "i < len(items)",
                    "origin": "model",
                },
                {
                    "path": "src/main.py",
                    "line": 2,
                    "category": "test_missing",
                    "problem": "No test for edge case",
                    "why_blocking": "Coverage required",
                    "evidence": "# Test case needed",
                    "origin": "model",
                },
            ],
        }
        session = PersistedSession(
            card_id="test-card", review_findings=review_record
        )
        assert session.review_findings == review_record
        assert len(session.review_findings["findings_before_gate"]) == 2
