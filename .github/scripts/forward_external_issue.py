"""Forward an external issue submission to the configured destinations (spec 152 / #210).

Public issues are the only contribution channel coordinare accepts — spec 142
closed public pull requests by design. This is what makes that promise real:
somebody sees a stranger's report without watching the repository by hand.

**It copies; it never composes.** The issue body is attacker-authored text, and an
AI summary of it delivered to a human's inbox is a prompt-injection surface for
very little gain. Every field here is passed through verbatim, bounded in length,
and quoted so the reader can always tell the submitter's words from ours. A
forwarder that only copies cannot mislead anyone about what the issue says.

**It is a script rather than inline ``actions/github-script``** so the things that
matter — verbatim-ness, visible truncation, quoting, skip-versus-fail — can be
tested with a synthetic payload instead of only by an outsider filing an issue.

Triage is not done here. The advocate role already decides what reaches the
board; this is a notification, and growing it into a second triage brain would
duplicate judgment that lives elsewhere.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

#: How much of the body to forward. Enough to triage from, short enough that a
#: pasted logfile does not become the notification.
BODY_LIMIT = 2000

#: Appended when the body is cut. Silence here would let the extract read as the
#: complete report, which is the one thing a forwarder must never imply.
TRUNCATION_NOTICE = "\n\n[truncated — open the issue for the rest]"

REQUEST_TIMEOUT_SECONDS = 15


class NoDestinationsConfigured(RuntimeError):
    """Nothing is configured, so this run would forward nowhere.

    Deliberately fatal. A green run that delivered nothing is indistinguishable
    from a working forwarder until somebody notices a report was missed, which is
    the failure this whole feature exists to prevent.
    """


class DeliveryFailed(RuntimeError):
    """A destination rejected the message. Names which one."""


@dataclass(frozen=True, slots=True)
class Extract:
    """The verbatim, length-bounded projection of a submission."""

    title: str
    author: str
    url: str
    number: int
    body: str
    labels: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class Destination:
    name: str
    config: dict[str, str]


def build_extract(issue: dict[str, Any]) -> Extract:
    """Project an issue payload into what will be forwarded.

    Every field is copied. Nothing is normalised, corrected or filled in: an empty
    body forwards as empty, because inventing a placeholder is writing prose about
    a submission we did not read.
    """
    body = issue.get("body") or ""
    if len(body) > BODY_LIMIT:
        body = body[:BODY_LIMIT] + TRUNCATION_NOTICE

    return Extract(
        title=issue.get("title") or "",
        author=(issue.get("user") or {}).get("login") or "",
        url=issue.get("html_url") or "",
        number=int(issue.get("number") or 0),
        body=body,
        labels=[label.get("name", "") for label in issue.get("labels") or []],
    )


def render_slack(extract: Extract) -> dict[str, Any]:
    """A Slack payload. Submission text goes in ``plain_text``, which Slack never parses.

    The first version fenced the body and escaped the fence delimiter. That is a
    losing game and it lost: a body of five backticks left an odd number behind,
    and the submitter's text ended up outside the quote, in the message's own
    voice — able to read as a maintainer note. Escaping a delimiter means winning
    every variation; ``plain_text`` means the delimiter has no meaning at all.

    Only our own framing is ``mrkdwn``. Nothing the submitter wrote is.
    """
    labels = f" [{', '.join(extract.labels)}]" if extract.labels else ""
    framing = (
        f"*New external issue* #{extract.number}{labels}\n"
        f"submitted by `{extract.author}` — {extract.url}"
    )
    return {
        # Notification fallback. Deliberately carries no submitter text: it is the
        # one string Slack renders outside the blocks.
        "text": f"New external issue #{extract.number}",
        "blocks": [
            {"type": "section", "text": {"type": "mrkdwn", "text": framing}},
            {
                "type": "section",
                "text": {"type": "plain_text", "text": f"Title: {extract.title}"},
            },
            {
                "type": "section",
                "text": {
                    "type": "plain_text",
                    "text": extract.body or "(no body)",
                },
            },
        ],
    }


def _escape_html(text: str) -> str:
    return (
        text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
    )


def render_email(extract: Extract) -> str:
    """An email body. Submission text is escaped, never live markup in an inbox."""
    # Escaped like every other submitter-controlled field. GitHub constrains label
    # names, but relying on that makes this the one field whose safety depends on
    # somebody else's validation rules staying as they are.
    labels_text = ", ".join(_escape_html(label) for label in extract.labels)
    labels = f" [{labels_text}]" if extract.labels else ""
    return (
        f"<p><strong>New external issue</strong> #{extract.number}{labels}<br>"
        f"submitted by <code>{_escape_html(extract.author)}</code><br>"
        f'<a href="{_escape_html(extract.url)}">{_escape_html(extract.url)}</a></p>'
        f"<p><strong>Title</strong></p><pre>{_escape_html(extract.title)}</pre>"
        f"<p><strong>Issue body (verbatim, may be truncated)</strong></p>"
        f"<pre>{_escape_html(extract.body)}</pre>"
    )


def _present(env: dict[str, str], key: str) -> str | None:
    """A secret's value, or ``None`` when it is absent or blank.

    A whitespace-only secret is an unfinished setup rather than a destination, and
    treating it as configured would forward into nothing while reporting success.
    """
    value = (env.get(key) or "").strip()
    return value or None


def resolve_destinations(env: dict[str, str]) -> list[Destination]:
    """Every destination that is fully configured.

    Partially configured destinations are absent, not broken: an email API key
    with no recipient is somebody halfway through setup, and guessing a recipient
    is not available to us.
    """
    destinations: list[Destination] = []

    webhook = _present(env, "SLACK_WEBHOOK_URL")
    if webhook:
        destinations.append(Destination(name="slack", config={"url": webhook}))

    api_key = _present(env, "ISSUE_FORWARD_EMAIL_API_KEY")
    to_address = _present(env, "ISSUE_FORWARD_EMAIL_TO")
    from_address = _present(env, "ISSUE_FORWARD_EMAIL_FROM")
    if api_key and to_address and from_address:
        destinations.append(
            Destination(
                name="email",
                config={"api_key": api_key, "to": to_address, "from": from_address},
            )
        )

    if not destinations:
        raise NoDestinationsConfigured(
            "no forwarding destination is configured, so this issue would reach nobody. "
            "Set SLACK_WEBHOOK_URL, or all of ISSUE_FORWARD_EMAIL_API_KEY / "
            "ISSUE_FORWARD_EMAIL_TO / ISSUE_FORWARD_EMAIL_FROM, as repository secrets."
        )
    return destinations


def _post(url: str, payload: dict[str, Any], headers: dict[str, str]) -> None:
    request = urllib.request.Request(  # noqa: S310 - https URLs from repository secrets
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", **headers},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:  # noqa: S310
        if response.status >= 300:
            msg = f"HTTP {response.status}"
            raise RuntimeError(msg)


def send(destination: Destination, extract: Extract) -> None:
    """Deliver to one destination. Injected in tests, so no test touches the network."""
    if destination.name == "slack":
        _post(destination.config["url"], render_slack(extract), {})
        return
    if destination.name == "email":
        _post(
            "https://api.resend.com/emails",
            {
                "from": destination.config["from"],
                "to": [destination.config["to"]],
                "subject": f"[coordinare] external issue #{extract.number}: {extract.title}"[:200],
                "html": render_email(extract),
            },
            {"Authorization": f"Bearer {destination.config['api_key']}"},
        )
        return
    msg = f"unknown destination {destination.name!r}"
    raise DeliveryFailed(msg)


def deliver(
    destinations: list[Destination],
    extract: Extract,
    *,
    sender: Callable[[Destination, Extract], None] = send,
) -> None:
    """Send to every destination, failing the run on the first rejection.

    Named in the failure, because "delivery failed" without saying where sends an
    operator to check both.
    """
    for destination in destinations:
        try:
            sender(destination, extract)
        except Exception as exc:
            msg = f"delivery to {destination.name!r} failed: {exc}"
            raise DeliveryFailed(msg) from exc
        print(f"forwarded to {destination.name}")  # noqa: T201 - workflow log


def main() -> int:
    event_path = os.environ.get("GITHUB_EVENT_PATH")
    if not event_path:
        print("GITHUB_EVENT_PATH is unset; nothing to forward", file=sys.stderr)  # noqa: T201
        return 1

    with open(event_path, encoding="utf-8") as handle:
        event = json.load(handle)

    extract = build_extract(event.get("issue") or {})
    destinations = resolve_destinations(dict(os.environ))

    configured = {d.name for d in destinations}
    for name in ("slack", "email"):
        if name not in configured:
            print(f"{name}: not configured, skipping")  # noqa: T201

    deliver(destinations, extract)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
