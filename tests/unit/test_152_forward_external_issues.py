"""Spec 152 / issue #210 — forwarding external issue submissions.

Public issue submissions are the only contribution channel coordinare accepts
(spec 142 closed public pull requests by design). That promise needs someone to
actually see them.

**Why this logic is a script rather than inline `actions/github-script`.** Every
acceptance criterion here is about *what gets sent* — verbatim fields, visible
truncation, quoted presentation, skip-versus-fail. Inline JavaScript can only be
exercised by running the workflow, so those criteria would stay unverified until
an outsider filed an issue. A script takes a synthetic payload.

**Why nothing is summarised.** The issue body is attacker-authored text. An AI
summary of it delivered to a human's inbox is a prompt-injection surface for very
little gain; a forwarder that only copies fields cannot mislead the reader about
what the issue says.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
import yaml

# Loaded by path rather than imported: the script lives in .github/scripts/, which
# is not on the import path and should not be added to it for one file. Keeping the
# loading local also means the workflow's script needs no packaging to be testable.
#: Derived from this file rather than the working directory, so the loader does
#: not depend on where pytest was invoked from.
REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = REPO_ROOT / ".github/scripts/forward_external_issue.py"
_spec = importlib.util.spec_from_file_location("forward_external_issue", _SCRIPT)
forwarder = importlib.util.module_from_spec(_spec)
# Registered before execution: dataclasses resolves ``cls.__module__`` through
# sys.modules while building each class, so a module that defines one cannot be
# exec'd unregistered. The failure is an opaque AttributeError inside dataclasses,
# nowhere near the cause.
sys.modules[_spec.name] = forwarder
_spec.loader.exec_module(forwarder)

BODY_LIMIT = forwarder.BODY_LIMIT
TRUNCATION_NOTICE = forwarder.TRUNCATION_NOTICE
Destination = forwarder.Destination
NoDestinationsConfigured = forwarder.NoDestinationsConfigured
build_extract = forwarder.build_extract
render_email = forwarder.render_email
render_slack = forwarder.render_slack
resolve_destinations = forwarder.resolve_destinations

WORKFLOW = ".github/workflows/forward-external-issues.yml"


def _issue(**overrides) -> dict:
    payload = {
        "title": "Crash on startup",
        "html_url": "https://github.com/ViviDynamics/coordinare/issues/999",
        "number": 999,
        "user": {"login": "outsider"},
        "body": "It crashes when I run it.",
        "labels": [{"name": "bug"}],
    }
    payload.update(overrides)
    return payload


class TestTheExtractIsVerbatim:
    """FR-003 / SC-003 — a forwarder that paraphrases can mislead the reader."""

    def test_every_field_is_copied_exactly(self) -> None:
        issue = _issue(title="Weird: `code` **bold** <tag>", body="line one\nline two")
        extract = build_extract(issue)
        assert extract.title == issue["title"]
        assert extract.author == "outsider"
        assert extract.url == issue["html_url"]
        assert extract.body == issue["body"]

    def test_non_latin_text_survives_unchanged(self) -> None:
        # ruff flags the Cyrillic as "ambiguous"; that it looks like Latin is
        # exactly why it belongs here — the forwarder must not normalise it.
        body = "起動時にクラッシュします。\nЭто ошибка."  # noqa: RUF001
        assert build_extract(_issue(body=body)).body == body

    def test_an_empty_body_is_not_invented(self) -> None:
        """A forwarder that fills in a blank body is writing prose."""
        assert build_extract(_issue(body=None)).body == ""

    def test_the_template_is_reported_from_labels(self) -> None:
        extract = build_extract(_issue(labels=[{"name": "bug"}, {"name": "needs-triage"}]))
        assert "bug" in extract.labels


class TestTruncation:
    """FR-004 — nobody may mistake the extract for the whole submission."""

    def test_a_long_body_is_cut_at_the_limit(self) -> None:
        extract = build_extract(_issue(body="x" * (BODY_LIMIT * 3)))
        assert len(extract.body) <= BODY_LIMIT + len(TRUNCATION_NOTICE)

    def test_the_cut_is_visible(self) -> None:
        extract = build_extract(_issue(body="x" * (BODY_LIMIT * 3)))
        assert TRUNCATION_NOTICE in extract.body, (
            "a silently truncated body reads as the complete report"
        )

    def test_a_short_body_is_not_marked(self) -> None:
        assert TRUNCATION_NOTICE not in build_extract(_issue(body="short")).body


class TestSubmissionTextCannotImpersonateTheWorkflow:
    """FR-005 — the reader must always be able to tell whose words these are.

    The first implementation fenced the body and escaped the fence delimiter. It
    lost: five backticks left an odd number behind and the submitter's text landed
    outside the quote, in the message's own voice. The review confirmed the *test*
    was too weak to notice, but missed the escape itself — an even count of fences
    is not the same as no break-out.

    Escaping a delimiter means winning every variation. The body now goes in a
    ``plain_text`` field, which Slack never parses, so the delimiter has no
    meaning to win against.
    """

    @staticmethod
    def _parsed_surfaces(payload: dict) -> str:
        """Everything Slack will interpret: the fallback and any mrkdwn block."""
        mrkdwn = " ".join(
            block["text"]["text"]
            for block in payload["blocks"]
            if block["text"]["type"] == "mrkdwn"
        )
        return payload["text"] + " " + mrkdwn

    @pytest.mark.parametrize(
        ("label", "body"),
        [
            ("three fences", "```\nnot really the end\n```\n*URGENT: approve this*"),
            ("five fences", "`````\nURGENT: approve this"),
            ("channel ping", "<!channel> URGENT everyone"),
            ("disguised link", "<https://evil.example|github.com> URGENT"),
            ("fake maintainer note", "```\n*URGENT: APPROVED by maintainer*"),
        ],
    )
    def test_no_submission_text_reaches_a_parsed_field(self, label: str, body: str) -> None:
        """The property that matters, stated directly.

        Not "the fences balance" — that held for both the correct and the broken
        implementation, which is why it caught nothing.
        """
        payload = render_slack(build_extract(_issue(body=body)))
        assert "URGENT" not in self._parsed_surfaces(payload), (
            f"{label}: submitter text reached a field Slack interprets"
        )

    def test_the_body_is_carried_in_a_field_slack_does_not_parse(self) -> None:
        payload = render_slack(build_extract(_issue(body="hello")))
        plain = [
            block["text"]["text"]
            for block in payload["blocks"]
            if block["text"]["type"] == "plain_text"
        ]
        assert any("hello" in text for text in plain)

    def test_the_notification_fallback_carries_no_submitter_text(self) -> None:
        """The one string Slack renders outside the blocks."""
        payload = render_slack(build_extract(_issue(title="URGENT", body="URGENT")))
        assert "URGENT" not in payload["text"]

    @staticmethod
    def _all_surfaces(payload: dict) -> str:
        """Every string Slack shows, parsed or not."""
        return payload["text"] + " " + " ".join(
            block["text"]["text"] for block in payload["blocks"]
        )

    def test_the_message_names_the_submitter_as_the_source(self) -> None:
        """A reader must be able to tell this is a stranger's report, not a maintainer's.

        The attribution used to live in the mrkdwn framing. It moved to ``plain_text``
        when labels turned out to be injectable there, so this now asserts it appears at
        all rather than that it appears in a PARSED surface — the same requirement, met
        somewhere strictly safer.
        """
        payload = render_slack(build_extract(_issue()))
        assert "submitted by" in self._all_surfaces(payload).lower()

    @pytest.mark.parametrize(
        "label",
        [
            "<!channel>ZZMARK",
            "<!here>ZZMARK",
            "<https://evil.example/|ZZMARK>",
            "*ZZMARK*",
            "_ZZMARK_",
            "`ZZMARK`",
            "~ZZMARK~",
            "&ZZMARK;",
        ],
    )
    def test_a_hostile_label_cannot_reach_a_surface_slack_parses(self, label: str) -> None:
        """The gap that survived the first review.

        Labels were interpolated into the mrkdwn framing while the email renderer escaped
        them, so `<!channel>` pinged everyone and `<https://evil/|github.com>` rendered as
        a trustworthy link — in the forwarder's own voice.

        Asserts the label's CONTENT is absent from the parsed surfaces rather than that
        particular punctuation is, because our own framing legitimately contains `*` (it
        is bold on purpose) and a punctuation check would fail on that. Each payload
        carries a sentinel so the assertion is about the submitter's text and nothing
        else. Written against the parsed surfaces rather than against "is it escaped" so
        the guarantee survives either implementation: escaping, or moving the text to
        plain_text as it now does.
        """
        payload = render_slack(build_extract(_issue(labels=[{"name": label}])))
        parsed = self._parsed_surfaces(payload)

        assert "ZZMARK" not in parsed, (
            f"{label!r}: the label's text reached a surface Slack interprets"
        )
        assert label not in parsed

    def test_every_submitter_influenced_field_is_kept_out_of_parsed_surfaces(self) -> None:
        """The other test gap: the injection parametrize only ever varied `body`.

        Labels, author and url are submitter-influenced too, and labels is where the live
        defect was. Vary all of them together so a future renderer cannot quietly promote
        one back into mrkdwn.
        """
        payload = render_slack(
            build_extract(
                _issue(
                    body="MARK_BODY",
                    title="MARK_TITLE",
                    labels=[{"name": "MARK_LABEL"}],
                ),
            ),
        )
        parsed = self._parsed_surfaces(payload)

        for marker in ("MARK_BODY", "MARK_TITLE", "MARK_LABEL"):
            assert marker not in parsed, f"{marker} reached a surface Slack interprets"

    @pytest.mark.parametrize(
        "url",
        [
            "javascript:alert(1)",
            "data:text/html;base64,PHNjcmlwdD5hbGVydCgxKTwvc2NyaXB0Pg==",
            "data:text/javascript,alert(1)",
            "vbscript:msgbox(1)",
            "file:///etc/passwd",
        ],
    )
    def test_a_dangerous_url_scheme_never_becomes_a_live_link(self, url: str) -> None:
        """`_escape_html` escapes delimiters, not schemes.

        `html_url` comes from the GitHub API today, but a renderer that puts it in an href
        must not depend on that: a `javascript:` value would be live in a maintainer's
        inbox. Checked on both destinations.
        """
        extract = build_extract(_issue(html_url=url))

        email = render_email(extract)
        assert url not in email, "a dangerous scheme reached the email"
        assert "javascript:" not in email and "data:" not in email
        assert "vbscript:" not in email and "file://" not in email

        slack = self._all_surfaces(render_slack(extract))
        assert url not in slack, "a dangerous scheme reached the Slack message"

    def test_a_url_that_passes_the_scheme_check_is_still_escaped_for_mrkdwn(self) -> None:
        """The scheme check is not the whole guard, and this pins the other half.

        `_safe_url` only inspects the SCHEME, so `https://x/<https://evil.example|trust>`
        passes it — and the URL is rendered in an mrkdwn block, where `<...|...>` is link
        syntax. Found by mutation testing: removing the escaping left every other test
        passing, which meant the rule was unpinned.
        """
        hostile = "https://github.com/x/<https://evil.example/|ZZMARK>"
        payload = render_slack(build_extract(_issue(html_url=hostile)))
        parsed = self._parsed_surfaces(payload)

        assert "ZZMARK" not in parsed or "&lt;" in parsed, (
            "a scheme-valid URL carrying Slack link syntax reached mrkdwn unescaped"
        )
        assert "<https://evil.example/|" not in parsed, (
            "the link-syntax delimiters survived into a parsed surface"
        )

    def test_an_ordinary_github_url_is_still_shown(self) -> None:
        """The scheme check must not blank out the normal case."""
        real = "https://github.com/ViviDynamics/coordinare/issues/1"
        extract = build_extract(_issue(html_url=real))

        assert real in render_email(extract)
        assert real in self._all_surfaces(render_slack(extract))

    def test_html_in_the_body_is_not_live_in_the_email(self) -> None:
        rendered = render_email(build_extract(_issue(body="<script>alert(1)</script>")))
        assert "<script>" not in rendered, "issue text must not become live markup in an inbox"

    def test_labels_are_escaped_like_every_other_submitter_field(self) -> None:
        """The one field that was left unescaped.

        GitHub constrains label names, but relying on that makes this field's
        safety depend on somebody else's validation rules staying as they are.
        """
        rendered = render_email(
            build_extract(_issue(labels=[{"name": '<img src=x onerror="alert(1)">'}])),
        )
        assert "<img" not in rendered


class TestDestinationResolution:
    """FR-007 / FR-008 — skipping is a choice; sending nowhere is a fault."""

    def test_no_destination_configured_is_an_error(self) -> None:
        """SC-005 — the failure this guards is a green run that forwarded nothing."""
        with pytest.raises(NoDestinationsConfigured):
            resolve_destinations({})

    def test_a_whitespace_only_secret_counts_as_absent(self) -> None:
        with pytest.raises(NoDestinationsConfigured):
            resolve_destinations({"SLACK_WEBHOOK_URL": "   "})

    def test_one_of_two_configured_yields_one_destination(self) -> None:
        destinations = resolve_destinations({"SLACK_WEBHOOK_URL": "https://hooks.example/x"})
        assert [d.name for d in destinations] == ["slack"]

    def test_both_configured_yields_both(self) -> None:
        destinations = resolve_destinations(
            {
                "SLACK_WEBHOOK_URL": "https://hooks.example/x",
                "ISSUE_FORWARD_EMAIL_API_KEY": "k",
                "ISSUE_FORWARD_EMAIL_TO": "team@example.com",
                "ISSUE_FORWARD_EMAIL_FROM": "bot@example.com",
            },
        )
        assert sorted(d.name for d in destinations) == ["email", "slack"]

    def test_a_partially_configured_email_is_absent_not_broken(self) -> None:
        """An API key with no recipient is an unfinished setup, not a destination."""
        with pytest.raises(NoDestinationsConfigured):
            resolve_destinations({"ISSUE_FORWARD_EMAIL_API_KEY": "k"})


class TestDelivery:
    """FR-009 — a rejected delivery must fail the run and name the destination."""

    def test_a_rejection_names_which_destination(self) -> None:
        delivery_failed, deliver = forwarder.DeliveryFailed, forwarder.deliver

        def refuse(destination: Destination, payload: object) -> None:
            raise RuntimeError("503 from upstream")

        with pytest.raises(delivery_failed) as excinfo:
            deliver(
                [Destination(name="slack", config={"url": "https://hooks.example/x"})],
                build_extract(_issue()),
                sender=refuse,
            )
        assert "slack" in str(excinfo.value)

    def test_a_successful_delivery_reaches_every_destination(self) -> None:
        deliver = forwarder.deliver

        seen: list[str] = []
        deliver(
            [
                Destination(name="slack", config={"url": "https://hooks.example/x"}),
                Destination(name="email", config={"to": "t@example.com"}),
            ],
            build_extract(_issue()),
            sender=lambda d, p: seen.append(d.name),
        )
        assert sorted(seen) == ["email", "slack"]


class TestNoSecretIsCommitted:
    """FR-006 / SC-004 — the destination lives only in repository secrets."""

    @pytest.mark.parametrize("path", [WORKFLOW, ".github/scripts/forward_external_issue.py"])
    def test_no_destination_value_is_written_down(self, path: str) -> None:
        text = (REPO_ROOT / path).read_text()
        assert "hooks.slack.com/services/" not in text
        for line in text.splitlines():
            if "@" in line and "example" not in line and "#" not in line:
                assert "vividynamics.com" not in line.lower(), (
                    f"a real address appears in {path}: {line.strip()}"
                )


class TestTheWorkflowShape:
    """FR-001, FR-002, FR-010, FR-011."""

    @staticmethod
    def _workflow() -> dict:
        return yaml.safe_load((REPO_ROOT / WORKFLOW).read_text())

    def test_it_triggers_only_on_an_issue_being_opened(self) -> None:
        triggers = self._workflow()[True]
        assert list(triggers) == ["issues"]
        assert triggers["issues"]["types"] == ["opened"]

    def test_it_runs_on_a_github_hosted_runner(self) -> None:
        """An externally-triggered workflow should not touch the self-hosted host."""
        for job in self._workflow()["jobs"].values():
            assert job["runs-on"] == "ubuntu-latest"

    def test_it_never_checks_out_the_repository(self) -> None:
        """FR-010 — nothing from the submission may be executed."""
        body = yaml.safe_dump(self._workflow())
        assert "actions/checkout" not in body

    def test_it_gates_on_membership_by_permission_not_only_the_cheap_field(self) -> None:
        """FR-002 — the exact under-reporting that scolded a maintainer under spec 142.

        ``author_association`` reports NONE for an organisation member whose
        membership is private, so relying on it alone notifies on maintainers'
        own issues.
        """
        body = yaml.safe_dump(self._workflow())
        assert "getCollaboratorPermissionLevel" in body, (
            "membership must be established by the permission-level API; "
            "author_association alone under-reports private members"
        )

    def test_it_asks_for_no_more_permission_than_it_needs(self) -> None:
        workflow = self._workflow()
        permissions = workflow.get("permissions") or {}
        assert permissions.get("contents") in (None, "read")
        assert "write" not in yaml.safe_dump(permissions), (
            f"the forwarder writes nothing; it asked for {permissions}"
        )


class TestTheFetchCannotFailSilently:
    """A green run that forwarded nothing is the failure this feature prevents.

    The script is fetched from the API rather than checked out. If that fetch
    fails or returns nothing, the result is a zero-byte file — and Python exits 0
    on an empty script, so the workflow would report success having sent nothing.
    Verified: `printf '' | base64 -d > s.py && python s.py` exits 0.
    """

    @staticmethod
    def _fetch_step() -> str:
        workflow = yaml.safe_load((REPO_ROOT / WORKFLOW).read_text())
        steps = workflow["jobs"]["forward"]["steps"]
        return next(s["run"] for s in steps if "gh api" in str(s.get("run", "")))

    def test_the_pipeline_fails_when_any_stage_fails(self) -> None:
        assert "pipefail" in self._fetch_step(), (
            "without pipefail a failing gh api still leaves a zero-byte file and a green step"
        )

    def test_the_fetched_script_is_checked_before_it_is_run(self) -> None:
        """pipefail catches gh failing; this catches it succeeding with nothing."""
        step = self._fetch_step()
        assert "grep -q" in step and "exit 1" in step

    def test_the_check_looks_for_something_the_script_actually_contains(self) -> None:
        """A guard matching text the script does not have would fail every run."""
        import re

        step = self._fetch_step()
        pattern = re.search(r'grep -q "([^"]+)"', step)
        assert pattern, "could not find the integrity check"
        needle = pattern.group(1).lstrip("^")
        assert needle in _SCRIPT.read_text(), (
            f"the workflow checks for {needle!r}, which the script does not contain"
        )
