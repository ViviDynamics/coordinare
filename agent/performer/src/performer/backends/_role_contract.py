"""The closing instruction of a task prompt, by role.

Verdict roles answer with one JSON object that ``main.py`` parses; working roles
commit their changes and leave the push to the performer. The wording matches
the closing that codex, opencode and junie each write inline today.
"""
from __future__ import annotations

from performer.models import DIAGNOSTIC_ROLE, LOCAL_CAPTURE_RULE

#: Roles whose report is a JSON object read from the backend's final output.
JSON_ONLY_ROLES = frozenset({
    "assessing",
    "assessor",
    "reviewing",
    "reviewer",
    "closing_review",
    "closer",
    "security",
    "qa",
    "documenting",
    "tech_writer",
})

_QA_REMINDERS = (
    "QA contract reminder: include a non-empty `verification_steps` array.",
    # 391: models that found the bug still lost it, writing failures as strings
    # and leaving failed commands out of executed_checks. State the shapes.
    (
        'Record every command you ran in `executed_checks`, including the ones that failed: '
        '"executed_checks": [{"command": "<exact command>", "exit_code": <integer>, '
        '"output": "<short excerpt>"}]. A failing command is evidence too.'
    ),
    (
        'Every defect you found goes in `failures`, one object per defect: '
        '"failures": [{"criterion": "<acceptance criterion>", "expected": "<expected behaviour>", '
        '"actual": "<what you observed>", "test": "<command or test that shows it>"}]. '
        "Set `passed` to false when `failures` is not empty."
    ),
    "For bug fixes, label steps as verification (not reproduction) unless explicitly asked.",
    "Set `visual_validation_required=true` for UI/UX/visual changes and capture at least one artifact in `visual_evidence` for those tasks.",
    "Include `visual_evidence` entries when screenshots/GIFs/videos/artifacts are available.",
    "Include exact capture attempts in `visual_capture_commands` (commands/scripts you ran).",
    LOCAL_CAPTURE_RULE,
    "If visual evidence cannot be captured, include `demo_setup_steps` and `visual_capture_blockers` with concrete details.",
    (
        "Screenshot uploads: after capturing a screenshot to disk, run "
        "`performer-upload-screenshot <path>` (available on $PATH). It "
        "uploads the file to GitHub's user-attachments CDN and prints "
        "an https://github.com/user-attachments/assets/... URL on stdout. "
        "Use that URL as `path_or_url` in your `visual_evidence` entry so "
        "the image renders inline in the PR comment. If the upload command "
        "is unavailable or fails, keep the local path — the performer "
        "wrapper will retry the upload before posting."
    ),
)


def role_closing_section(score: object) -> list[str]:
    """The last lines of the task prompt for *score*'s role."""
    role = getattr(score, "role", None)
    if role == DIAGNOSTIC_ROLE:
        return [
            "This is a one-off diagnostic/benchmark task. Use any tools at your "
            "disposal to complete it. You do NOT need to commit, push, or open a "
            "pull request — just perform the task and report what you did.",
        ]
    if role in JSON_ONLY_ROLES:
        parts = [
            "Return ONLY a valid JSON object for your role contract.",
            "Do not include markdown, prose, or code fences.",
        ]
        if role == "qa":
            parts += list(_QA_REMINDERS)
        return parts
    return [
        "Complete the task above. Commit your changes with a clear, descriptive commit message.",
        "Do not push or open a pull request — this will be handled automatically after you finish.",
    ]
