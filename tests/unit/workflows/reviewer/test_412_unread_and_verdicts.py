"""412: empty diffs, unread files and the advisory tier are explicit.

The reviewer used to pass vacuously on a diff it never parsed (coverage over
zero files is trivially complete) and the sanitizer's truncation cut the tail
without naming what it cut. These tests hold the three fixes: the
nothing_to_review short-circuit, named unread files, and advisory findings that
post without blocking.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from performer.workflows.reviewer.diffparse import (
    _UNNAMED_TAIL_PATH,
    parse_unified_diff,
    unread_beyond_truncation,
    unread_names_from_note,
)
from performer.workflows.reviewer.gate import (
    add_documentation_by_implementer,
    blocking_of,
    cap_findings,
    full_coverage,
    run_gate,
    verdict,
)
from performer.workflows.reviewer.models import ChangedFile, Hunk, ReviewRecord
from performer.workflows.reviewer.post import build_review

from tests.unit.workflows.reviewer._fixtures import DIFF, changed_files, finding


def _hunk(start: int) -> Hunk:
    return Hunk(header=f"@@ -{start},1 +{start},2 @@", start_line=start, end_line=start + 1, lines=[])


def _file(path: str, start: int = 1) -> ChangedFile:
    return ChangedFile(path=path, hunks=[_hunk(start)], fully_in_diff=True, opened_by_survey=False)


# --- diffparse: real header parsing (was parts[3]) -------------------------

def test_header_path_with_spaces():
    text = (
        "diff --git a/my docs/read me.md b/my docs/read me.md\n"
        "--- a/my docs/read me.md\n+++ b/my docs/read me.md\n"
        "@@ -1,1 +1,2 @@\n old\n+new\n"
    )
    files = parse_unified_diff(text)
    assert [f.path for f in files] == ["my docs/read me.md"]
    assert files[0].fully_in_diff is True


def test_rename_only_entry_is_not_fully_in_diff():
    text = (
        "diff --git a/old_name.py b/new_name.py\n"
        "similarity index 100%\n"
        "rename from old_name.py\n"
        "rename to new_name.py\n"
    )
    files = parse_unified_diff(text)
    assert [f.path for f in files] == ["new_name.py"]
    assert files[0].fully_in_diff is False, "a rename with no hunks carries no readable content"


# --- truncation: named unread files ---------------------------------------

_NAMED_NOTE = (
    "[coordinare: omitted 0 tooling/vendor and 0 binary file section(s); "
    "diff truncated to 60000 chars; unread beyond this point: 2 file(s); "
    "cut off inside: 1 file(s)]\n"
    "coordinare-unread: src/late.py\n"
    "coordinare-unread: src/later.py\n"
    "coordinare-cut: src/mid.py"
)


def test_unread_names_from_note_extracts_both_parts():
    names = unread_names_from_note(f"head text\n\n{_NAMED_NOTE}\n")
    assert names == ["src/late.py", "src/later.py", "src/mid.py"]


def test_unread_names_from_note_are_verbatim():
    """412 round 24: the capture is not stripped -- the sanitizer emits the
    path exactly, and .strip() would corrupt a valid name with leading or
    trailing spaces until it no longer matches the ChangedFile the coverage
    gates hold on."""
    names = unread_names_from_note(
        "[coordinare: diff truncated to 10 chars]\n"
        "coordinare-unread:  leading space.py\n"
        "coordinare-cut: trailing.py \n",
    )
    assert names == [" leading space.py", "trailing.py "]


def test_unread_beyond_truncation_marks_and_adds_phantom_files():
    files = parse_unified_diff(DIFF)
    out = unread_beyond_truncation(files, DIFF + "\n" + _NAMED_NOTE)
    by_path = {f.path: f for f in out}
    assert by_path["src/mid.py"].fully_in_diff is False, "the cut-through file is not fully read"
    phantom = [f for f in out if f.path in ("src/late.py", "src/later.py")]
    assert len(phantom) == 2, "files entirely beyond the cap enter the changed set"
    assert all(f.hunks == [] and f.fully_in_diff is False for f in phantom)


def test_bare_truncation_note_falls_back_to_last_file():
    bare = "[coordinare: diff truncated to 60000 chars]"
    files = parse_unified_diff(DIFF)
    out = unread_beyond_truncation(files, DIFF + "\n" + bare)
    assert out[-1].fully_in_diff is False
    assert out[0].fully_in_diff is True


def test_bare_truncation_note_holds_on_the_unnamed_tail():
    """412 round 12: a bare note gives no trustworthy account of the hidden
    tail -- an unopenable phantom keeps coverage failing even after the
    cut-through file itself is opened, so the review cannot pass on the
    visible subset."""
    bare = "[coordinare: diff truncated to 60000 chars]"
    files = parse_unified_diff(DIFF)
    out = unread_beyond_truncation(files, DIFF + "\n" + bare)
    phantom = [f for f in out if f.path == "<unnamed files beyond the truncated diff>"]
    assert len(phantom) == 1, "the unnamed tail enters the changed set exactly once"
    assert phantom[0].fully_in_diff is False
    assert phantom[0].opened_by_survey is False
    visible_opened = [f for f in out if f.path != "<unnamed files beyond the truncated diff>"]
    visible_opened = [f.model_copy(update={"opened_by_survey": True}) for f in visible_opened]
    assert full_coverage(visible_opened + phantom, truncated=True, coverage_pass_ran=True) is False, (
        "opening every visible file must not certify the unnamed remainder"
    )


def test_unread_beyond_truncation_without_note_marks_last_file_unread():
    """The bare-note fallback: the sanitizer cuts the tail, so the last file
    is the one cut through."""
    files = parse_unified_diff(DIFF)
    out = unread_beyond_truncation(files, DIFF)
    assert out[-1].fully_in_diff is False
    assert out[0].fully_in_diff is True


# --- verdict: the advisory tier never blocks -------------------------------

def test_verdict_blocks_only_on_non_advisory_findings():
    advisory = finding(category="style")
    assert verdict([advisory], coverage_ok=True) == "approved", "advisory findings post, never block"
    assert verdict([advisory], coverage_ok=False) == "env_blocked"
    assert verdict([finding(category="logic_error")], coverage_ok=True) == "changes_requested"


def test_blocking_of_splits_the_tiers():
    kept = [finding(category="style"), finding(category="test_missing"), finding()]
    blocking = blocking_of(kept)
    assert [f.category for f in blocking] == ["logic_error"]


def test_run_gate_records_blocking_alongside_findings():
    outcome = run_gate(
        [finding(category="style"), finding()],
        [], changed_files=changed_files(), prior_comments=[],
        diff_lines=DIFF.splitlines(), survey_lines=[], brief=None,
        truncated=False, coverage_pass_ran=False,
    )
    assert outcome.verdict == "changes_requested"
    assert len(outcome.blocking) == 1 and outcome.blocking[0].category == "logic_error"
    assert [f.category for f in outcome.findings] == ["logic_error", "style"], "the cap reorders blocking first"


def test_run_gate_remaps_finding_indexes_across_the_cap_reorder():
    """412 round 7: cap_findings reorders blocking first; a persisted
    not_fixed disposition must keep pointing at the same finding."""
    blocking = finding()
    advisory = finding(category="style")
    comments = [SimpleNamespace(id="c1")]
    outcome = run_gate(
        [advisory, blocking],
        [SimpleNamespace(prior_comment_id="c1", status="not_fixed", finding_index=0)],
        changed_files=changed_files(), prior_comments=comments,
        diff_lines=DIFF.splitlines(), survey_lines=[], brief=None,
        truncated=False, coverage_pass_ran=False,
    )
    assert [f.category for f in outcome.findings] == ["logic_error", "style"]
    assert outcome.dispositions[0].finding_index == 1, "the cited advisory moved from 0 to 1 under the blocking-first cap"
    outcome = run_gate(
        [advisory, blocking],
        [SimpleNamespace(prior_comment_id="c1", status="not_fixed", finding_index=1)],
        changed_files=changed_files(), prior_comments=comments,
        diff_lines=DIFF.splitlines(), survey_lines=[], brief=None,
        truncated=False, coverage_pass_ran=False,
    )
    assert outcome.dispositions[0].finding_index == 0, "the cited blocking finding moved from 1 to 0"


def test_cap_keeps_blocking_findings_when_advisory_fills_the_budget():
    advisory = [finding(category="style", path=f"a{i}.py", line=1, evidence="e") for i in range(30)]
    blocking = finding()
    out = cap_findings([*advisory, blocking])
    assert len(out) == 30 and out[0].category == "logic_error", "blocking leads; it cannot be pushed past the cap"
    assert blocking_of(out) == [blocking] and verdict(out, coverage_ok=True) == "changes_requested"


def test_code_brief_with_docs_only_diff_still_finds():
    """A code-scoped brief whose diff touches only documentation is a lane
    violation, not a deliverable: only a docs-scoped brief skips."""
    files = [_file("docs/guide.md")]
    brief = {"modules": ["src/calc.py"]}
    out = add_documentation_by_implementer([], files, brief)
    assert [f.category for f in out] == ["documentation_by_implementer"]


def test_docs_scoped_brief_uses_the_serialized_module_path():
    """Brief modules arrive as serialized dicts ({path, note}), not strings --
    ``str(m)`` never matches a documentation path, so the skip never fired."""
    files = [_file("docs/guide.md")]
    brief = {"modules": [{"path": "docs/guide.md", "note": "writer's lane"}]}
    assert add_documentation_by_implementer([], files, brief) == []


def test_docs_scoped_brief_with_code_changes_still_finds():
    """The exception is a docs-scoped brief with a docs-only diff: reaching
    into code is out of the docs lane and is flagged."""
    files = [_file("docs/guide.md"), _file("src/app.py", start=8)]
    brief = {"modules": [{"path": "docs/guide.md", "note": "writer's lane"}]}
    out = add_documentation_by_implementer([], files, brief)
    assert [f.category for f in out] == ["documentation_by_implementer"]


# --- post: advisory lines are not blocking lines ----------------------------

def test_advisory_lines_do_not_say_why_blocking():
    advisory = [finding(category="style")]
    event, body, _inline = build_review([], advisory, changed_files(), [], 1, "header")
    assert event == "COMMENT" and "Advisory notes" in body
    assert "Why blocking" not in body


# --- survey: quoted paths count as opened -----------------------------------

def test_quoted_paths_with_spaces_count_as_opened():
    from performer.workflows.reviewer.survey import command_names_path

    assert command_names_path("cat 'my docs/read me.md'", "my docs/read me.md")
    assert command_names_path("git show 'HEAD:docs/my guide.md'", "docs/my guide.md")
    assert not command_names_path("cat src/calc.py", "my docs/read me.md")


# --- diffparse: truncation before the first header ---------------------------

def test_truncation_before_first_header_still_yields_phantoms():
    """A diff cut before its first ``diff --git`` header parses to nothing, but
    the note names every omitted file -- phantoms, so coverage holds unread."""
    diff = (
        "[coordinare: diff truncated to 100 chars — run `gh pr diff <pr_url>` for the full changes; "
        "unread beyond this point: 2 file(s)]\n"
        "coordinare-unread: src/a.py\n"
        "coordinare-unread: src/b.py\n"
    )
    out = unread_beyond_truncation([], diff)
    assert [f.path for f in out] == ["src/a.py", "src/b.py"]
    assert all(not f.fully_in_diff and not f.opened_by_survey for f in out)


def test_parse_without_a_note_stays_empty():
    assert unread_beyond_truncation([], "[coordinare: nothing omitted]") == []


def test_note_names_with_commas_stay_whole():
    """412 round 4: one path per machine line -- a comma inside a path is no
    longer a separator, so the real changed file stays fully_in_diff."""
    names = unread_names_from_note("[coordinare: diff truncated to 10 chars]\ncoordinare-unread: src/a,b.py\n")
    assert names == ["src/a,b.py"]


# --- diffparse: the +++ b/ line names the target exactly ---------------------

def test_parse_target_with_b_slash_comes_from_the_plus_header():
    """412 round 4: a path containing " b/" defeats the header's greedy split
    (the LAST " b/" sits inside the path); the exact target is the "+++ b/"
    line, which the header can never fool."""
    text = (
        "diff --git a/foo b/bar.md b/foo b/bar.md\n"
        "--- a/foo b/bar.md\n+++ b/foo b/bar.md\n"
        "@@ -1,1 +1,2 @@\n old\n+new\n"
    )
    files = parse_unified_diff(text)
    assert [f.path for f in files] == ["foo b/bar.md"]
    assert files[0].fully_in_diff is True


def test_added_line_starting_with_plus_plus_does_not_rename_the_file():
    """Inside a hunk, "+++ x" is an added line whose content starts with "++";
    only the file-section marker, which always precedes any hunk, sets the
    target."""
    text = (
        "diff --git a/real.py b/real.py\n"
        "--- a/real.py\n+++ b/real.py\n"
        "@@ -1,1 +1,2 @@\n old\n+++ b/decoy.py\n"
    )
    files = parse_unified_diff(text)
    assert [f.path for f in files] == ["real.py"]


def test_pure_deletion_keeps_the_header_target():
    """A deletion carries "+++ /dev/null"; the header's b/ side is the only
    name available, and it stays."""
    text = "diff --git a/gone.py b/gone.py\n--- a/gone.py\n+++ /dev/null\n@@ -1,1 +0,0 @@\n-old\n"
    files = parse_unified_diff(text)
    assert [f.path for f in files] == ["gone.py"]


def test_quoted_delete_only_header_is_unambiguous_and_flags_deleted():
    """412 round 8: git quotes BOTH header fields when the path needs it, and
    a delete-only section has no "+++" line to refine the header. The quoted
    form is parsed exactly -- no greedy " b/" split -- and the entry is
    flagged deleted."""
    text = (
        'diff --git "a/foo b/bar.md" "b/foo b/bar.md"\n'
        'deleted file mode 100644\n'
        '--- "a/foo b/bar.md"\n'
        '+++ /dev/null\n'
        '@@ -1,1 +0,0 @@\n'
        '-old\n'
    )
    files = parse_unified_diff(text)
    assert [f.path for f in files] == ["foo b/bar.md"]
    assert files[0].deleted is True


def test_unquoted_delete_only_path_with_b_slash_comes_from_the_minus_header():
    """412 round 9: a delete-only section with an unquoted " b/" path has no
    "+++" line, so the header's greedy split alone would reduce
    "foo b/bar.md" to "bar.md"; the exact "--- a/<path>" line wins."""
    text = (
        "diff --git a/foo b/bar.md b/foo b/bar.md\n"
        "deleted file mode 100644\n"
        "--- a/foo b/bar.md\n"
        "+++ /dev/null\n"
        "@@ -1,1 +0,0 @@\n"
        "-old\n"
    )
    files = parse_unified_diff(text)
    assert [f.path for f in files] == ["foo b/bar.md"]
    assert files[0].deleted is True


def test_mode_only_path_with_b_slash_is_parsed_exactly():
    """412 round 10: a mode-only section has no ---/+++ lines, so the header
    is the only name source. Its sides are the SAME path, and "a/<X> b/<X>"
    parses exactly even when X contains " b/" -- the greedy split alone would
    reduce it to "bar.md"."""
    text = (
        "diff --git a/docs/foo b/bar.md b/docs/foo b/bar.md\n"
        "old mode 100644\n"
        "new mode 100755\n"
    )
    files = parse_unified_diff(text)
    assert [f.path for f in files] == ["docs/foo b/bar.md"]


def test_delete_section_cut_before_the_hunk_is_not_marked_deleted():
    """412 round 10, reshaped in round 18: a bare `+++ /dev/null` section
    with no hunks reads as the empty-file deletion it is -- a cut between the
    header and the @@ is indistinguishable from it at the parser, and real
    sanitizer cuts are always named in the note, which the handler clears
    back to unread (test_truncation_clears_the_deleted_exemption...)."""
    text = "diff --git a/gone.py b/gone.py\n--- a/gone.py\n+++ /dev/null\n"
    files = parse_unified_diff(text)
    assert [f.path for f in files] == ["gone.py"]
    assert files[0].deleted is True
    assert files[0].fully_in_diff is False


def test_truncation_clears_the_deleted_exemption_for_cut_through_deletions():
    """412 round 11: a deletion section cut through AFTER its @@ header parses
    with deleted=True -- at parse time the cut is invisible. The truncation
    paths must clear the deletion exemption too, so the unseen removal body
    holds instead of passing as covered."""
    cut_diff = (
        "diff --git a/gone.py b/gone.py\n"
        "deleted file mode 100644\n"
        "--- a/gone.py\n"
        "+++ /dev/null\n"
        "@@ -1,1 +0,0 @@\n"
        "-old\n"
    )
    files = parse_unified_diff(cut_diff)
    assert files[0].deleted is True, "at parse time the cut is invisible"

    bare = "[coordinare: diff truncated to 60000 chars]"
    out = unread_beyond_truncation(files, cut_diff + "\n" + bare)
    assert out[0].fully_in_diff is False
    assert out[0].deleted is False

    named = (
        "[coordinare: omitted 0 tooling/vendor and 0 binary file section(s); "
        "diff truncated to 60000 chars; unread beyond this point: 0 file(s); "
        "cut off inside: 1 file(s)]\n"
        "coordinare-cut: gone.py"
    )
    out = unread_beyond_truncation(files, cut_diff + "\n" + named)
    assert out[0].deleted is False, "the named cut path clears the exemption as well"


def test_full_coverage_holds_a_cut_through_deletion():
    """412 round 11: a complete deletion is covered by the diff's own removals
    (round 8), but one cut through by truncation lost its exemption -- the
    removal body was never in the diff, so coverage must hold."""
    complete = ChangedFile(path="a.py", hunks=[], fully_in_diff=False, deleted=True)
    cut = ChangedFile(path="b.py", hunks=[], fully_in_diff=False, deleted=False)
    assert full_coverage([complete], truncated=False, coverage_pass_ran=False) is True
    assert full_coverage([cut], truncated=False, coverage_pass_ran=False) is False


def test_rename_only_target_with_b_slash_comes_from_rename_to():
    """412 round 5: a rename-only section has no "+++ b/" line, so the header
    greedy split alone would reduce "foo b/bar.md" to "bar.md"; the exact
    target is the "rename to" line."""
    text = (
        "diff --git a/foo b/bar.md b/foo b/bar.md\n"
        "similarity index 100%\n"
        "rename from foo b/bar.md\n"
        "rename to foo b/bar.md\n"
    )
    files = parse_unified_diff(text)
    assert [f.path for f in files] == ["foo b/bar.md"]


def test_copy_only_target_with_b_slash_comes_from_copy_to():
    """412 round 14: copy-only sections carry "copy to" instead of a "+++"
    line -- same ambiguity, same metadata authority as renames."""
    text = (
        "diff --git a/src-file.py b/copy b/dest.py\n"
        "similarity index 100%\n"
        "copy from src-file.py\n"
        "copy to copy b/dest.py\n"
    )
    files = parse_unified_diff(text)
    assert [f.path for f in files] == ["copy b/dest.py"]


def test_quoted_header_escapes_are_decoded():
    """412 round 15: git escapes the quoted payload (\\\" for a quote, octal
    for non-ASCII bytes); the real path is decoded, not the escaped
    spelling. Real output for quo"te.py as a mode-only quoted section."""
    text = (
        'diff --git "a/quo\\"te.py" "b/quo\\"te.py"\n'
        "old mode 100644\n"
        "new mode 100755\n"
    )
    files = parse_unified_diff(text)
    assert [f.path for f in files] == ['quo"te.py']


def test_quoted_rename_header_decodes_the_b_side():
    """412 round 16: a quoted rename's sides differ -- the identical-side
    backreference failed to match and the greedy split mangled the target;
    the b-side is decoded independently."""
    text = (
        'diff --git "a/old name.py" "b/new name.py"\n'
        "similarity index 100%\n"
        "rename from old name.py\n"
        "rename to new name.py\n"
    )
    files = parse_unified_diff(text)
    assert [f.path for f in files] == ["new name.py"]


def test_quoted_header_tab_escapes_are_decoded():
    """412 round 16: git escapes a real tab as \\\\t inside the quoted
    payload -- the decode must yield the tab, not drop the backslash."""
    text = (
        'diff --git "a/ta\\tb.py" "b/ta\\tb.py"\n'
        "old mode 100644\n"
        "new mode 100755\n"
    )
    files = parse_unified_diff(text)
    assert [f.path for f in files] == ["ta\tb.py"]


def test_quoted_header_octal_escapes_are_decoded():
    """412 round 15: non-ASCII names are octal-escaped UTF-8 bytes under
    core.quotePath (real git output for путь/файл.py)."""
    text = (
        'diff --git "a/\\320\\277\\321\\203\\321\\202\\321\\214/\\321\\204\\320\\260\\320\\271\\320\\273.py" '
        '"b/\\320\\277\\321\\203\\321\\202\\321\\214/\\321\\204\\320\\260\\320\\271\\320\\273.py"\n'
        "old mode 100644\n"
        "new mode 100755\n"
    )
    files = parse_unified_diff(text)
    assert [f.path for f in files] == ["путь/файл.py"]


def test_context_line_looking_like_a_note_is_not_metadata():
    """412 round 5: a hunk's context line " coordinare-unread: x" is file
    content -- the machine lines are column-0 only, so no phantom appears."""
    text = (
        "diff --git a/real.py b/real.py\n"
        "--- a/real.py\n+++ b/real.py\n"
        "@@ -1,1 +1,2 @@\n coordinare-unread: src/other.py\n+new\n"
    )
    files = parse_unified_diff(text)
    assert [f.path for f in files] == ["real.py"]
    assert unread_names_from_note(
        "diff --git a/real.py b/real.py\n@@ -1,1 +1,2 @@\n coordinare-unread: src/other.py\n",
    ) == []


# --- docs-scoped brief skips the lane violation ----------------------------

def test_docs_scoped_brief_skips_documentation_finding():
    files = [_file("docs/guide.md")]
    brief = {"modules": ["docs/guide.md"]}
    assert add_documentation_by_implementer([], files, brief) == []


def test_docs_scoped_brief_skips_finding_despite_the_truncation_tail():
    """412 round 17: the synthetic tail is a coverage marker, not a path --
    it is not documentation, but it must not defeat the docs-scoped
    exception; the truncation is held by the coverage gates instead."""
    files = [
        _file("docs/guide.md"),
        ChangedFile(path=_UNNAMED_TAIL_PATH, hunks=[], fully_in_diff=False, opened_by_survey=False),
    ]
    brief = {"modules": ["docs/guide.md"]}
    assert add_documentation_by_implementer([], files, brief) == []


def test_code_brief_with_docs_touch_still_finds():
    files = [_file("docs/guide.md"), _file("src/calc.py")]
    brief = {"modules": ["src/calc.py"]}
    out = add_documentation_by_implementer([], files, brief)
    assert [f.category for f in out] == ["documentation_by_implementer"]


# --- post: advisory-only reviews are comments ------------------------------

def test_build_review_comment_when_only_advisory():
    event, body, inline = build_review([], [finding(category="style")], changed_files(), [], 2, "H")
    assert event == "COMMENT" and inline == []
    assert "Advisory notes" in body and "style" in body


def test_build_review_changes_when_blocking():
    event, body, inline = build_review([finding()], [finding(category="style")], changed_files(), [], 2, "H")
    assert event == "REQUEST_CHANGES" and len(inline) == 1
    assert "Advisory notes" in body


# --- the record verdict and workflow short-circuit --------------------------

def test_review_record_accepts_nothing_to_review():
    record = ReviewRecord(
        changed_files=[], diff_truncated=False, unread_files=[],
        verdict="nothing_to_review", covered_files=[],
    )
    assert record.verdict == "nothing_to_review"


@pytest.mark.asyncio
async def test_empty_diff_short_circuits_to_nothing_to_review():
    """The workflow returns the verdict with zero model calls and no review."""
    from performer.workflows.base import WorkflowMetrics
    from performer.workflows.reviewer import ReviewerWorkflow
    from performer.workflows.toolkit import Toolkit

    events: list = []
    tk = Toolkit(
        metrics=WorkflowMetrics(),
        model_call=_no_model_call,
        command_runner=_clean_runner,
        event_sink=events.append,
        call_limit=4,
    )
    score = SimpleNamespace(
        pr_diff="", prior_comments=[], implementation_brief={"work_kind": "feature"},
        title="t", description="d", pr_url="https://github.com/o/r/pull/1",
        owner_repo=("o", "r"), effective_github_token="tok", backend="codex", model="m",
        workflow_env={},
    )
    result = await ReviewerWorkflow().run(SimpleNamespace(path="/tmp/x"), score, tk)
    assert result.report["review"]["verdict"] == "nothing_to_review"
    assert result.report["workflow_metrics"]["model_calls"] == 0, "no model call on an empty diff"


async def _no_model_call(persona, content, max_tokens):  # pragma: no cover - must not be reached
    raise AssertionError("model call on an empty diff")


@pytest.mark.asyncio
async def test_fetch_outage_holds_instead_of_nothing_to_review():
    """A fetch outage is not an empty diff: hold with a reason, never advance."""
    from performer.workflows.base import WorkflowMetrics
    from performer.workflows.reviewer import ReviewerWorkflow
    from performer.workflows.toolkit import Toolkit

    tk = Toolkit(
        metrics=WorkflowMetrics(),
        model_call=_no_model_call,
        command_runner=_clean_runner,
        event_sink=lambda e: None,
        call_limit=4,
    )
    score = SimpleNamespace(
        pr_diff="", pr_diff_status="failed", prior_comments=[], implementation_brief={"work_kind": "feature"},
        title="t", description="d", pr_url="https://github.com/o/r/pull/1",
        owner_repo=("o", "r"), effective_github_token="tok", backend="codex", model="m",
        workflow_env={},
    )
    result = await ReviewerWorkflow().run(SimpleNamespace(path="/tmp/x"), score, tk)
    record = result.report["review"]
    assert record["verdict"] == "env_blocked"
    assert "could not be fetched" in record["post_error"]


@pytest.mark.asyncio
async def test_truncated_before_first_header_holds_instead_of_nothing_to_review():
    """412 round 5: a truncated diff with no machine names hides an unknown
    unread set -- hold, never a vacuous empty-diff advance."""
    from performer.workflows.base import WorkflowMetrics
    from performer.workflows.reviewer import ReviewerWorkflow
    from performer.workflows.toolkit import Toolkit

    tk = Toolkit(
        metrics=WorkflowMetrics(),
        model_call=_no_model_call,
        command_runner=_clean_runner,
        event_sink=lambda e: None,
        call_limit=4,
    )
    score = SimpleNamespace(
        pr_diff="[coordinare: diff truncated to 10 chars — run `gh pr diff <pr_url>` for the full changes]",
        prior_comments=[], implementation_brief={"work_kind": "feature"},
        title="t", description="d", pr_url="https://github.com/o/r/pull/1",
        owner_repo=("o", "r"), effective_github_token="tok", backend="codex", model="m",
        workflow_env={},
    )
    result = await ReviewerWorkflow().run(SimpleNamespace(path="/tmp/x"), score, tk)
    record = result.report["review"]
    assert record["verdict"] == "env_blocked"
    assert "truncated" in record["post_error"]


@pytest.mark.asyncio
async def test_unread_overflow_holds_instead_of_reviewing_a_partial_set():
    """412 round 6: paths beyond the note budget are declared by count; an
    unknown-size unread set holds, coverage cannot be verified on a subset."""
    from performer.workflows.base import WorkflowMetrics
    from performer.workflows.reviewer import ReviewerWorkflow
    from performer.workflows.toolkit import Toolkit

    tk = Toolkit(
        metrics=WorkflowMetrics(),
        model_call=_no_model_call,
        command_runner=_clean_runner,
        event_sink=lambda e: None,
        call_limit=4,
    )
    score = SimpleNamespace(
        pr_diff=(
            "[coordinare: diff truncated to 10 chars — run `gh pr diff <pr_url>` for the full changes; "
            "unread beyond this point: 5 file(s)]\ncoordinare-unread-overflow: 5\n"
        ),
        prior_comments=[], implementation_brief={"work_kind": "feature"},
        title="t", description="d", pr_url="https://github.com/o/r/pull/1",
        owner_repo=("o", "r"), effective_github_token="tok", backend="codex", model="m",
        workflow_env={},
    )
    result = await ReviewerWorkflow().run(SimpleNamespace(path="/tmp/x"), score, tk)
    record = result.report["review"]
    assert record["verdict"] == "env_blocked"
    assert "could not name 5 more unread file(s)" in record["post_error"]


async def _clean_runner(cmd, cwd, timeout_s):
    return 0, "" if "git status" in cmd else "x"
