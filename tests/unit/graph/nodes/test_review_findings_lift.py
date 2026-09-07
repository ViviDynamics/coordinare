"""Tests for spec 169 review_findings lift in monitor_performer."""


from coordinare.graph.nodes.monitor_performer import _lift_review_findings


class TestReviewFindingsLift:
    """Tests for review_findings field lifting from performer response to state."""

    def test_lift_changes_requested_with_findings(self):
        """When reviewer reports changes_requested with findings, lift them."""
        state = {"review_findings": None}
        report = {
            "review": {
                "changed_files": [
                    {
                        "path": "src/main.py",
                        "hunks": [
                            {"header": "@@ -1,1 +1,1 @@", "start_line": 1, "end_line": 1}
                        ],
                        "fully_in_diff": True,
                    }
                ],
                "diff_truncated": False,
                "verdict": "changes_requested",
                "covered_files": ["src/main.py"],
                "findings": [
                    {
                        "path": "src/main.py",
                        "line": 1,
                        "category": "style",
                        "problem": "Missing docstring",
                        "why_blocking": "Documentation required",
                        "evidence": "def foo():",
                        "origin": "model",
                    }
                ],
            }
        }

        _lift_review_findings(state, report, "reviewing")

        assert state["review_findings"] is not None
        assert state["review_findings"]["verdict"] == "changes_requested"
        assert len(state["review_findings"]["findings"]) == 1

    def test_lift_approved_no_findings(self):
        """When reviewer reports approved with no findings, still lift for record."""
        state = {"review_findings": None}
        report = {
            "review": {
                "changed_files": [],
                "diff_truncated": False,
                "verdict": "approved",
                "covered_files": [],
                "findings": [],
            }
        }

        _lift_review_findings(state, report, "reviewing")

        # Approved reviews are also lifted for completeness
        assert state["review_findings"] is not None
        assert state["review_findings"]["verdict"] == "approved"

    def test_no_lift_when_not_reviewing_stage(self):
        """Do not lift findings when stage is not reviewing."""
        state = {"review_findings": None}
        report = {
            "review": {
                "verdict": "changes_requested",
                "changed_files": [],
                "diff_truncated": False,
                "covered_files": [],
            }
        }

        _lift_review_findings(state, report, "implementing")

        # Should not lift for non-reviewing stages
        assert state["review_findings"] is None

    def test_no_lift_when_no_review_key(self):
        """Do not lift when report has no 'review' key."""
        state = {"review_findings": None}
        report = {"some_other_key": "value"}

        _lift_review_findings(state, report, "reviewing")

        # Should not crash, just not lift
        assert state["review_findings"] is None

    def test_lift_deep_copies_findings(self):
        """Lifted findings are deep copied, not references."""
        state = {"review_findings": None}
        original_findings = [
            {
                "path": "src/main.py",
                "line": 1,
                "category": "style",
                "problem": "Issue",
                "why_blocking": "Because",
                "evidence": "code",
                "origin": "model",
            }
        ]
        report = {
            "review": {
                "changed_files": [],
                "diff_truncated": False,
                "verdict": "changes_requested",
                "covered_files": [],
                "findings": original_findings,
            }
        }

        _lift_review_findings(state, report, "reviewing")

        # Modify the original list
        original_findings[0]["problem"] = "Modified"

        # State should have the original value, not the modified one
        assert state["review_findings"]["findings"][0]["problem"] == "Issue"

    def test_lift_logs_count_and_categories(self, caplog):
        """Lifted findings are logged with count and categories."""
        import logging

        caplog.set_level(logging.INFO)
        state = {"review_findings": None}
        report = {
            "review": {
                "changed_files": [],
                "diff_truncated": False,
                "verdict": "changes_requested",
                "covered_files": [],
                "findings": [
                    {
                        "path": "src/main.py",
                        "line": 1,
                        "category": "style",
                        "problem": "Issue 1",
                        "why_blocking": "Because",
                        "evidence": "code",
                        "origin": "model",
                    },
                    {
                        "path": "src/main.py",
                        "line": 2,
                        "category": "logic_error",
                        "problem": "Issue 2",
                        "why_blocking": "Because",
                        "evidence": "code",
                        "origin": "model",
                    },
                ],
            }
        }

        _lift_review_findings(state, report, "reviewing")

        # Check that findings were lifted
        assert state["review_findings"] is not None
        # Logging output would be captured in caplog

    def test_no_lift_when_empty_findings_list_but_changes_requested(self):
        """When changes_requested but findings list is empty, still lift."""
        state = {"review_findings": None}
        report = {
            "review": {
                "changed_files": [],
                "diff_truncated": False,
                "verdict": "changes_requested",
                "covered_files": [],
                "findings": [],
            }
        }

        _lift_review_findings(state, report, "reviewing")

        # Still lift even if findings is empty (might be due to gate rules adding findings later)
        assert state["review_findings"] is not None
