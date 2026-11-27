"""402: Scrutare as the reviewing stage's provider.

Speaks Scrutare's performer API as its ``docs/app.md`` describes it:

- ``POST /api/reviews`` with ``Authorization: Bearer <token>`` and
  ``{"repository", "number", "installation", "request_id"}`` returns 202 with the job.
  The same request id returns the same job; a request id reused for another target is 409.
- ``GET /api/jobs/<id>`` returns the job. ``queued`` and ``running`` are in flight;
  ``posted`` and ``unchanged`` carry the CLI result document (``head_sha``, ``verdict``
  of ``approve``, ``changes_requested`` or ``escalated``, and for ``posted`` the review
  receipt). ``failed``, ``interrupted`` and ``not_started`` carry ``delivery``:
  ``not_sent`` is safe to request again, ``confirmed`` and ``uncertain`` are not.

Nothing here changes Scrutare or depends on anything beyond that published API.
"""
from __future__ import annotations

import os
import re
from http import HTTPStatus
from typing import TYPE_CHECKING, Any

import httpx
import structlog

from coordinare.services.stage_contract import (
    JobState,
    StageJob,
    StageOutcome,
    StageRequest,
    StageVerdict,
)
from coordinare.transport.base import TransportError
from coordinare.transport.http_transport import PerformerAuthError

if TYPE_CHECKING:
    from coordinare.stage_provider_config import ScrutareProviderConfig

logger = structlog.get_logger(__name__)

_VERDICTS = {
    "approve": StageVerdict.PASSED,
    "changes_requested": StageVerdict.CHANGES_REQUESTED,
    "escalated": StageVerdict.NEEDS_HUMAN,
}
_PENDING = frozenset({"queued", "running"})
_DONE = frozenset({"posted", "unchanged"})
_ENDED = frozenset({"failed", "interrupted", "not_started", "superseded"})
_JOB_ID = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
#: A job id that cannot exist, used to prove the token without creating anything.
_PROBE_JOB = "coordinare-credential-probe"


class ScrutareProvider:
    """A :class:`~coordinare.services.stage_contract.StageProvider` for Scrutare."""

    def __init__(
        self,
        name: str,
        config: ScrutareProviderConfig,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.name = name
        self._config = config
        self._transport = transport

    @property
    def config(self) -> ScrutareProviderConfig:
        return self._config

    async def submit(self, request: StageRequest, request_id: str) -> StageJob:
        body = {
            "repository": request.repository,
            "number": request.pr_number,
            "installation": self._config.installation,
            "request_id": request_id,
        }
        response = await self._send("POST", "/api/reviews", json=body)
        if response.status_code == HTTPStatus.CONFLICT:
            msg = (
                f"Scrutare refused request id {request_id}: it already names another pull "
                "request (409)"
            )
            raise TransportError(msg)
        self._raise_for_status(response, "submit a review")
        return self._job(response)

    async def poll(self, ref: str) -> StageJob:
        if not _JOB_ID.match(ref):
            msg = f"Scrutare job id {ref!r} is not one Scrutare issues"
            raise TransportError(msg)
        response = await self._send("GET", f"/api/jobs/{ref}")
        self._raise_for_status(response, f"read job {ref}")
        return self._job(response)

    async def health(self) -> tuple[bool, str]:
        """Answering and accepting the token: ``/healthz`` then an authenticated read."""
        live = await self._send("GET", "/healthz", authenticated=False)
        if live.status_code != HTTPStatus.OK:
            return False, f"GET /healthz answered {live.status_code}"
        probe = await self._send("GET", f"/api/jobs/{_PROBE_JOB}")
        if probe.status_code in (HTTPStatus.UNAUTHORIZED, HTTPStatus.FORBIDDEN):
            return False, f"Scrutare refused the token in {self._config.token_env} ({probe.status_code})"
        # Scrutare checks the token before it looks the job up, so its own "job not
        # found" proves the token. Any other 404 is a route that is not Scrutare's API
        # (for example the public webhook route, which does not serve /api/jobs).
        if probe.status_code != HTTPStatus.NOT_FOUND or _error_text(probe) != "job not found":
            return False, (
                f"GET /api/jobs answered {probe.status_code} ({_error_text(probe)}), not Scrutare's "
                "performer API; url must reach the App service that serves /api/jobs"
            )
        return True, "reachable, token accepted"

    # -- internals -----------------------------------------------------------------

    def _token(self) -> str:
        token = os.environ.get(self._config.token_env, "").strip()
        if not token:
            msg = (
                f"stage provider {self.name!r}: environment variable {self._config.token_env} "
                "is not set; it must hold Scrutare's performer API token"
            )
            raise PerformerAuthError(msg)
        return token

    async def _send(
        self, method: str, path: str, *, json: dict[str, Any] | None = None, authenticated: bool = True,
    ) -> httpx.Response:
        headers = {"Accept": "application/json"}
        if authenticated:
            headers["Authorization"] = f"Bearer {self._token()}"
        try:
            async with httpx.AsyncClient(
                base_url=self._config.url,
                timeout=self._config.request_timeout_seconds,
                transport=self._transport,
                follow_redirects=False,
            ) as client:
                return await client.request(method, path, json=json, headers=headers)
        except httpx.HTTPError as exc:
            msg = f"Scrutare at {self._config.url} is unreachable: {type(exc).__name__}: {exc}"
            raise TransportError(msg) from exc

    def _raise_for_status(self, response: httpx.Response, action: str) -> None:
        if response.status_code in (200, 202):
            return
        error = _error_text(response)
        if response.status_code in (401, 403):
            msg = (
                f"Scrutare refused to {action} ({response.status_code}: {error}); check "
                f"{self._config.token_env} and that installation {self._config.installation} "
                "is allowed for this repository"
            )
            raise PerformerAuthError(msg)
        msg = f"Scrutare could not {action} ({response.status_code}: {error})"
        raise TransportError(msg)

    def _job(self, response: httpx.Response) -> StageJob:
        try:
            record = response.json()
        except ValueError as exc:
            msg = "Scrutare answered with a body that is not JSON"
            raise TransportError(msg) from exc
        if not isinstance(record, dict) or not isinstance(record.get("id"), str) or not _JOB_ID.match(record["id"]):
            msg = "Scrutare answered without a job id"
            raise TransportError(msg)
        return job_from_record(record, review_login=self._config.review_login)


def job_from_record(record: dict[str, Any], *, review_login: str | None = None) -> StageJob:
    """Map one Scrutare job record onto the stage contract."""
    ref = str(record["id"])
    status = str(record.get("status") or "")
    result = _as_dict(record.get("result"))
    if status in _PENDING:
        return StageJob(ref=ref, state=JobState.PENDING, detail=status)
    if status in _DONE:
        outcome = _outcome(result, review_login=review_login)
        if outcome is None:
            # Scrutare itself treats an unusable success document as an uncertain outcome.
            return StageJob(
                ref=ref,
                state=JobState.NEEDS_HUMAN,
                detail=status,
                reason=(
                    f"job {status} but its result is not a usable review result; inspect the "
                    "job's artifacts before requesting another review"
                ),
            )
        return StageJob(ref=ref, state=JobState.DONE, outcome=outcome, detail=status)
    if status in _ENDED:
        delivery = str(result.get("delivery") or "")
        why = str(result.get("reason") or result.get("error") or "").strip()
        why = f": {why}" if why else ""
        if status == "superseded" or delivery == "not_sent":
            return StageJob(
                ref=ref,
                state=JobState.RETRYABLE,
                detail=status,
                reason=f"job {status}, nothing was posted{why}",
            )
        if delivery == "confirmed":
            reason = (
                f"job {status} after its review was delivered{why}. Scrutare says not to request "
                "another review; read the posted review and the job's artifacts, then move the card on"
            )
        elif delivery == "uncertain":
            reason = (
                f"job {status} and its review may have been posted{why}. Inspect posting.json in "
                "the job's retained run before requesting another review"
            )
        else:
            reason = f"job {status} without a delivery state{why}; inspect the job before retrying"
        return StageJob(ref=ref, state=JobState.NEEDS_HUMAN, detail=status, reason=reason)
    return StageJob(
        ref=ref,
        state=JobState.NEEDS_HUMAN,
        detail=status or "unknown",
        reason=f"job reports status {status!r}, which this version of coordinare does not know",
    )


def _outcome(result: dict[str, Any], *, review_login: str | None) -> StageOutcome | None:
    verdict = _VERDICTS.get(str(result.get("verdict") or ""))
    head = str(result.get("head_sha") or "")
    if verdict is None or not re.fullmatch(r"[0-9a-f]{40}", head):
        return None
    review = _as_dict(result.get("review"))
    commit = str(review.get("commit_id") or "")
    if commit and commit != head:
        # The receipt names the commit GitHub attached the review to; it must be the
        # head Scrutare captured, or the document contradicts itself.
        return None
    return StageOutcome(
        verdict=verdict,
        reviewed_head=head,
        summary=str(review.get("body") or ""),
        record_ref=str(review.get("html_url") or ""),
        author_login=str(review.get("login") or review_login or ""),
    )


def _error_text(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.reason_phrase or "no detail"
    if isinstance(body, dict) and isinstance(body.get("error"), str):
        return str(body["error"])[:200]
    return response.reason_phrase or "no detail"


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}
