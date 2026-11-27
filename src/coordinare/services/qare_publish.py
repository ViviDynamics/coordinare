"""404: post a QAre run's evidence where the built-in QA posts its own.

The built-in QA performer pushes its screenshots to the repository's orphan ``qa-assets``
branch and posts a "QA Evidence" comment on the pull request and on the work item. A
QAre run gets the same: its ``result.json``, ``plan.json`` and every evidence file the
result names are committed to ``qa-assets`` under ``qare/<request id>/``, and one comment
naming the head that was tested, every criterion's outcome and links to its evidence goes
on the pull request and the issue. QAre's own posting is off (``post: none``).

Every step can be repeated safely, so a publish that failed half way is simply run again
on the next poll: an upload already on the branch is not made twice, a comment carrying
the job's marker is not posted twice, and ``published.json`` in the job directory records
what is done.
"""
from __future__ import annotations

import base64
import json
from http import HTTPStatus
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any

import httpx
import structlog

from coordinare.lib.redaction import redact_secrets
from coordinare.services import qare_runner as runner
from coordinare.services.stage_contract import StageOutcome, StageVerdict
from coordinare.transport.base import TransportError

if TYPE_CHECKING:
    from coordinare.services.qare import GitHubAccess

logger = structlog.get_logger(__name__)

BRANCH = "qa-assets"
PUBLISHED_FILE = "published.json"
_MAX_FILES = 80
_MAX_FILE_BYTES = 5 * 1024 * 1024
_MAX_TOTAL_BYTES = 25 * 1024 * 1024
_IMAGES = {".png": "image", ".jpg": "image", ".jpeg": "image", ".gif": "image", ".webp": "image"}
_COMMENT_LIMIT = 60000
#: Evidence links shown per criterion in the table; the rest are counted.
_LINKS_PER_CRITERION = 6
_RESULT_LABEL = {
    "passed": "PASSED",
    "failed": "FAILED",
    "blocked": "BLOCKED (environment, not a defect)",
    "refused": "REFUSED",
    "waived": "WAIVED (held for a person)",
}
_OUTCOME_LABEL = {"proven": "proven", "failed": "**failed**", "unverified": "unverified"}


def marker(request_id: str) -> str:
    return f"<!-- coordinare-qare:{request_id} -->"


async def publish(
    job_dir: Path, spec: dict[str, Any], outcome: StageOutcome, github: GitHubAccess,
    *, transport: httpx.AsyncBaseTransport | None = None,
) -> StageOutcome:
    """Publish the job's evidence once, and return the outcome with links in place of paths."""
    done = _load(job_dir / PUBLISHED_FILE)
    if done is not None:
        return _linked(outcome, done)
    repository = str(spec["repository"])
    request_id = str(spec["request_id"])
    prefix = f"qare/{request_id}"
    try:
        token = await github.token(repository)
    except Exception as exc:
        msg = f"could not get a GitHub token to publish QAre evidence: {type(exc).__name__}"
        raise TransportError(msg) from exc
    api = github.api_base(repository)
    web = github.web_base(repository)
    async with httpx.AsyncClient(
        base_url=api, timeout=30, transport=transport, follow_redirects=False,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
    ) as client:
        files, skipped = collect_files(job_dir, outcome)
        await _upload(client, repository, prefix, files)
        record = {
            "prefix": prefix,
            "tree_url": f"{web}/{repository}/tree/{BRANCH}/{prefix}",
            "blob_base": f"{web}/{repository}/blob/{BRANCH}/{prefix}",
            "raw_base": f"{web}/{repository}/raw/{BRANCH}/{prefix}",
            "uploaded": sorted(files),
            "skipped": skipped,
        }
        linked = _linked(outcome, record)
        body = render_comment(spec, linked, record)
        pr_url = await _comment_once(client, repository, int(spec["pr_number"]), request_id, body)
        record["pr_comment_url"] = pr_url
        issue = int(spec.get("issue_number") or 0)
        if issue > 0 and issue != int(spec["pr_number"]):
            record["issue_comment_url"] = await _comment_once(client, repository, issue, request_id, body)
    runner._write_atomic(job_dir / PUBLISHED_FILE, json.dumps(record, indent=2))
    logger.info("qare.evidence_published", job=request_id, files=len(files), skipped=len(skipped), pr_comment=pr_url)
    return _linked(outcome, record)


def collect_files(job_dir: Path, outcome: StageOutcome) -> tuple[dict[str, bytes], list[str]]:
    """The files to upload, keyed by their path under the job's prefix, and what was left out.

    ``result.json`` and ``plan.json`` always; then every evidence path a criterion names,
    if it is a regular file inside the evidence directory. Text is redacted again before
    it leaves the host; binary files other than images are left out.
    """
    evidence = (job_dir / runner.EVIDENCE_DIR).resolve()
    wanted: list[tuple[str, Path]] = [
        ("result.json", evidence / "result.json"),
        ("plan.json", job_dir / runner.PLAN_FILE),
    ]
    for criterion in outcome.criteria:
        for ref in criterion.evidence:
            rel = _safe_relative(ref)
            if rel is not None:
                wanted.append((f"evidence/{rel}", evidence / rel))
    files: dict[str, bytes] = {}
    skipped: list[str] = []
    total = 0
    for name, path in wanted:
        if name in files:
            continue
        data = _readable(path, evidence if name.startswith("evidence/") else None)
        if data is None or len(files) >= _MAX_FILES or len(data) > _MAX_FILE_BYTES or total + len(data) > _MAX_TOTAL_BYTES:
            skipped.append(name)
            continue
        if PurePosixPath(name).suffix.lower() not in _IMAGES:
            try:
                data = redact_secrets(data.decode("utf-8")).encode("utf-8")
            except UnicodeDecodeError:
                skipped.append(name)
                continue
        files[name] = data
        total += len(data)
    return files, skipped


def render_comment(spec: dict[str, Any], outcome: StageOutcome, record: dict[str, Any]) -> str:
    """The evidence comment: the head tested, the verdict, and each criterion with its evidence."""
    head = str(spec.get("head_sha") or "")
    label = _RESULT_LABEL.get(outcome.provider_verdict, outcome.provider_verdict or str(outcome.verdict))
    if outcome.provider_verdict == "passed" and outcome.verdict is not StageVerdict.PASSED:
        label = "PASSED by QAre, held by coordinare (not every criterion is proven)"
    proven = sum(1 for c in outcome.criteria if c.outcome == "proven")
    lines = [
        marker(str(spec.get("request_id") or "")),
        "## QA Evidence (QAre)",
        "",
        f"**Result:** {label}",
        f"- Head tested: `{head}`",
        f"- Criteria proven: {proven} of {len(outcome.criteria)}",
        f"- Evidence: [{record['prefix']}]({record['tree_url']}) on the `{BRANCH}` branch",
        "",
        "| Criterion | Outcome | Evidence |",
        "| --- | --- | --- |",
    ]
    for criterion in outcome.criteria:
        text = _cell(criterion.text or criterion.id)
        result = _OUTCOME_LABEL.get(criterion.outcome, criterion.outcome)
        if criterion.reason:
            result += f": {_cell(redact_secrets(criterion.reason))}"
        shown = criterion.evidence[:_LINKS_PER_CRITERION]
        links = ", ".join(_link(ref) for ref in shown) or "none"
        if len(criterion.evidence) > len(shown):
            links += f", and {len(criterion.evidence) - len(shown)} more"
        lines.append(f"| {text} | {result} | {links} |")
    images = [ref for c in outcome.criteria for ref in c.evidence if _is_image_link(ref)]
    if images:
        lines += ["", "### Screenshots", ""]
        lines += [f"![{PurePosixPath(ref).name}]({ref})" for ref in images[:8]]
    if outcome.summary:
        lines += ["", redact_secrets(outcome.summary)]
    if record.get("skipped"):
        lines += ["", f"Not uploaded (too large, not text, or outside the evidence directory): "
                      f"{', '.join(f'`{s}`' for s in record['skipped'][:10])}"]
    body = "\n".join(lines)
    return body if len(body) <= _COMMENT_LIMIT else body[:_COMMENT_LIMIT] + "\n\n(truncated)"


def _linked(outcome: StageOutcome, record: dict[str, Any]) -> StageOutcome:
    uploaded = set(record.get("uploaded") or [])
    criteria = []
    for criterion in outcome.criteria:
        refs = []
        for ref in criterion.evidence:
            rel = _safe_relative(ref)
            name = f"evidence/{rel}" if rel is not None else ""
            if name in uploaded:
                base = record["raw_base"] if PurePosixPath(name).suffix.lower() in _IMAGES else record["blob_base"]
                refs.append(f"{base}/{name}")
            else:
                refs.append(ref)
        criteria.append(criterion.model_copy(update={"evidence": refs}))
    return outcome.model_copy(update={
        "criteria": criteria,
        "record_ref": str(record.get("pr_comment_url") or outcome.record_ref or record.get("tree_url") or ""),
    })


async def _upload(client: httpx.AsyncClient, repository: str, prefix: str, files: dict[str, bytes]) -> None:
    """Commit ``files`` under ``prefix`` on ``qa-assets`` in one commit, unless already there."""
    probe = await client.get(f"/repos/{repository}/contents/{prefix}/result.json", params={"ref": BRANCH})
    if probe.status_code == HTTPStatus.OK:
        return  # an earlier publish already committed this job's evidence
    _ok(probe, (200, 404), "read qa-assets")
    for _attempt in range(3):
        ref = await client.get(f"/repos/{repository}/git/ref/heads/{BRANCH}")
        _ok(ref, (200, 404), "read the qa-assets branch")
        parent = ref.json()["object"]["sha"] if ref.status_code == HTTPStatus.OK else None
        base_tree = None
        if parent:
            commit = await client.get(f"/repos/{repository}/git/commits/{parent}")
            _ok(commit, (200,), "read the qa-assets head")
            base_tree = commit.json()["tree"]["sha"]
        entries = []
        for name, data in sorted(files.items()):
            blob = await client.post(f"/repos/{repository}/git/blobs", json={
                "content": base64.b64encode(data).decode(), "encoding": "base64",
            })
            _ok(blob, (201,), "upload evidence")
            entries.append({"path": f"{prefix}/{name}", "mode": "100644", "type": "blob", "sha": blob.json()["sha"]})
        tree_body: dict[str, Any] = {"tree": entries}
        if base_tree:
            tree_body["base_tree"] = base_tree
        tree = await client.post(f"/repos/{repository}/git/trees", json=tree_body)
        _ok(tree, (201,), "build the evidence tree")
        commit = await client.post(f"/repos/{repository}/git/commits", json={
            "message": f"qa: QAre evidence for {prefix.rsplit('/', 1)[-1]}",
            "tree": tree.json()["sha"], "parents": [parent] if parent else [],
        })
        _ok(commit, (201,), "commit the evidence")
        sha = commit.json()["sha"]
        if parent:
            moved = await client.patch(f"/repos/{repository}/git/refs/heads/{BRANCH}", json={"sha": sha, "force": False})
        else:
            moved = await client.post(f"/repos/{repository}/git/refs", json={"ref": f"refs/heads/{BRANCH}", "sha": sha})
        if moved.status_code in (200, 201):
            return
        if moved.status_code != HTTPStatus.UNPROCESSABLE_ENTITY:
            _ok(moved, (200, 201), "move the qa-assets branch")
        # 422: someone else moved the branch first. Build on their commit and try again.
    msg = "the qa-assets branch kept moving while QAre evidence was committed"
    raise TransportError(msg)


async def _comment_once(
    client: httpx.AsyncClient, repository: str, number: int, request_id: str, body: str,
) -> str:
    """The URL of the job's comment on issue or pull request ``number``, posting it if absent."""
    tag = marker(request_id)
    url: str | None = f"/repos/{repository}/issues/{number}/comments"
    params: dict[str, Any] = {"per_page": 100}
    for _page in range(30):
        if url is None:
            break
        page = await client.get(url, params=params)
        _ok(page, (200,), f"read the comments on #{number}")
        for comment in page.json():
            if isinstance(comment, dict) and tag in str(comment.get("body") or ""):
                return str(comment.get("html_url") or "")
        url = page.links.get("next", {}).get("url")
        params = {}
    posted = await client.post(f"/repos/{repository}/issues/{number}/comments", json={"body": body})
    _ok(posted, (201,), f"comment on #{number}")
    return str(posted.json().get("html_url") or "")


def _ok(response: httpx.Response, expected: tuple[int, ...], action: str) -> None:
    if response.status_code not in expected:
        msg = f"GitHub refused to {action} ({response.status_code})"
        raise TransportError(msg)


def _safe_relative(ref: str) -> str | None:
    """A QAre evidence path, if it is relative and stays inside the evidence directory."""
    if not ref or ref.startswith(("http://", "https://")):
        return None
    path = PurePosixPath(ref.replace("\\", "/"))
    if not path.parts or path.is_absolute() or ".." in path.parts or ref.startswith("//") or ":" in path.parts[0]:
        return None
    return str(path)


def _readable(path: Path, inside: Path | None) -> bytes | None:
    try:
        if path.is_symlink() or not path.is_file():
            return None
        if inside is not None and not path.resolve().is_relative_to(inside):
            return None
        return path.read_bytes()
    except OSError:
        return None


def _load(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _cell(text: str) -> str:
    return " ".join(text.split()).replace("|", "\\|")[:400]


def _link(ref: str) -> str:
    if ref.startswith(("http://", "https://")):
        return f"[{PurePosixPath(ref).name}]({ref})"
    return f"`{_cell(ref)}`"


def _is_image_link(ref: str) -> bool:
    return ref.startswith(("http://", "https://")) and PurePosixPath(ref).suffix.lower() in _IMAGES


__all__ = ["BRANCH", "collect_files", "marker", "publish", "render_comment"]
