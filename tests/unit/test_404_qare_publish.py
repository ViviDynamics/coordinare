"""404: QAre evidence reaches qa-assets, the pull request and the issue, once."""
from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from coordinare.services import qare_publish
from coordinare.services.qare import GitHubAccess
from coordinare.services.stage_contract import CriterionOutcome, StageOutcome, StageVerdict
from coordinare.transport.base import TransportError

HEAD = "a" * 40
RID = f"coordinare.qa.0123456789abcdef.{HEAD}.1"
SECRET = "ghp_" + "Z" * 36


class FakeGitHubRest:
    """Enough of GitHub's REST API for the evidence branch and issue comments."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.branch: str | None = None
        self.files: dict[str, bytes] = {}
        self.comments: dict[int, list[dict[str, Any]]] = {}
        self.blobs: dict[str, bytes] = {}
        self.trees: dict[str, dict[str, str]] = {}
        self.commit_trees: dict[str, str] = {}
        self.conflicts = 0
        self.transport = httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append((request.method, path))
        body = json.loads(request.content) if request.content else {}
        if request.method == "GET" and "/contents/" in path:
            name = path.split("/contents/", 1)[1]
            return httpx.Response(200 if name in self.files else 404, json={})
        if path.endswith("/git/ref/heads/qa-assets"):
            if self.branch is None:
                return httpx.Response(404, json={})
            return httpx.Response(200, json={"object": {"sha": self.branch}})
        if request.method == "GET" and "/git/commits/" in path:
            return httpx.Response(200, json={"tree": {"sha": self.commit_trees[path.rsplit("/", 1)[1]]}})
        if path.endswith("/git/blobs"):
            sha = f"blob{len(self.blobs)}"
            self.blobs[sha] = base64.b64decode(body["content"])
            return httpx.Response(201, json={"sha": sha})
        if path.endswith("/git/trees"):
            sha = f"tree{len(self.trees)}"
            self.trees[sha] = {e["path"]: e["sha"] for e in body["tree"]}
            return httpx.Response(201, json={"sha": sha})
        if path.endswith("/git/commits"):
            sha = f"commit{len(self.commit_trees)}"
            self.commit_trees[sha] = body["tree"]
            return httpx.Response(201, json={"sha": sha, "tree": body["tree"]})
        if path.endswith(("/git/refs", "/git/refs/heads/qa-assets")):
            if self.conflicts:
                self.conflicts -= 1
                return httpx.Response(422, json={"message": "Update is not a fast forward"})
            self.branch = body["sha"]
            for name, blob in self.trees[self.commit_trees[body["sha"]]].items():
                self.files[name] = self.blobs[blob]
            return httpx.Response(200 if request.method == "PATCH" else 201, json={})
        if "/issues/" in path and path.endswith("/comments"):
            number = int(path.split("/issues/")[1].split("/")[0])
            if request.method == "GET":
                return httpx.Response(200, json=self.comments.get(number, []))
            comment = {"body": body["body"], "html_url": f"https://github.com/acme/widgets/issues/{number}#c{len(self.calls)}"}
            self.comments.setdefault(number, []).append(comment)
            return httpx.Response(201, json=comment)
        return httpx.Response(500, json={"message": f"unexpected {request.method} {path}"})


async def _token(repository: str) -> str:
    return "ghs_publishtoken"


ACCESS = GitHubAccess(_token, lambda repository: "https://api.github.com")


def _job(tmp_path: Path) -> tuple[Path, dict[str, Any], StageOutcome]:
    job = tmp_path / RID
    evidence = job / "evidence" / "checks" / "ac-1" / "1"
    evidence.mkdir(parents=True)
    (evidence / "stdout.txt").write_text(f"token={SECRET}\nok\n")
    (evidence / "screen.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    (job / "evidence" / "result.json").write_text('{"schemaVersion": "1"}')
    (job / "plan.json").write_text('{"schemaVersion": "1"}')
    (tmp_path / "outside.txt").write_text("not evidence")
    spec = {"request_id": RID, "repository": "acme/widgets", "pr_number": 7, "issue_number": 5, "head_sha": HEAD}
    outcome = StageOutcome(
        verdict=StageVerdict.CHANGES_REQUESTED, reviewed_head=HEAD, provider_verdict="failed",
        criteria=[
            CriterionOutcome(id="ac-1", text="The notice | shows", outcome="failed", reason="no notice",
                             evidence=["checks/ac-1/1/stdout.txt", "checks/ac-1/1/screen.png", "../outside.txt"]),
            CriterionOutcome(id="ac-2", text="The test passes", outcome="proven", evidence=["/etc/passwd"]),
        ],
    )
    return job, spec, outcome


@pytest.mark.asyncio
async def test_evidence_is_committed_and_linked_from_the_pr_and_the_issue(tmp_path: Path) -> None:
    gh = FakeGitHubRest()
    job, spec, outcome = _job(tmp_path)
    linked = await qare_publish.publish(job, spec, outcome, ACCESS, transport=gh.transport)
    prefix = f"qare/{RID}"
    assert sorted(gh.files) == sorted([
        f"{prefix}/result.json", f"{prefix}/plan.json",
        f"{prefix}/evidence/checks/ac-1/1/stdout.txt", f"{prefix}/evidence/checks/ac-1/1/screen.png",
    ])
    # Text is redacted again before it leaves the host; nothing outside the evidence goes.
    assert SECRET.encode() not in gh.files[f"{prefix}/evidence/checks/ac-1/1/stdout.txt"]
    assert ("POST", "/repos/acme/widgets/git/refs") in gh.calls  # the branch did not exist yet
    pr_body = gh.comments[7][0]["body"]
    assert gh.comments[5][0]["body"] == pr_body
    assert qare_publish.marker(RID) in pr_body
    assert HEAD in pr_body
    assert "**failed**: no notice" in pr_body
    assert "The notice \\| shows" in pr_body
    assert f"https://github.com/acme/widgets/raw/qa-assets/{prefix}/evidence/checks/ac-1/1/screen.png" in pr_body
    assert linked.record_ref == gh.comments[7][0]["html_url"]
    assert linked.criteria[0].evidence[0] == (
        f"https://github.com/acme/widgets/blob/qa-assets/{prefix}/evidence/checks/ac-1/1/stdout.txt"
    )
    assert linked.criteria[0].evidence[2] == "../outside.txt"
    assert (job / qare_publish.PUBLISHED_FILE).exists()


@pytest.mark.asyncio
async def test_publishing_again_changes_nothing(tmp_path: Path) -> None:
    gh = FakeGitHubRest()
    job, spec, outcome = _job(tmp_path)
    first = await qare_publish.publish(job, spec, outcome, ACCESS, transport=gh.transport)
    calls = len(gh.calls)
    again = await qare_publish.publish(job, spec, outcome, ACCESS, transport=gh.transport)
    assert len(gh.calls) == calls
    assert again.record_ref == first.record_ref
    assert again.criteria == first.criteria


@pytest.mark.asyncio
async def test_a_publish_interrupted_after_posting_does_not_post_twice(tmp_path: Path) -> None:
    gh = FakeGitHubRest()
    job, spec, outcome = _job(tmp_path)
    await qare_publish.publish(job, spec, outcome, ACCESS, transport=gh.transport)
    (job / qare_publish.PUBLISHED_FILE).unlink()  # the daemon stopped before recording it
    blobs = len(gh.blobs)
    await qare_publish.publish(job, spec, outcome, ACCESS, transport=gh.transport)
    assert len(gh.blobs) == blobs
    assert len(gh.comments[7]) == 1
    assert len(gh.comments[5]) == 1


@pytest.mark.asyncio
async def test_a_branch_that_moved_is_built_on(tmp_path: Path) -> None:
    gh = FakeGitHubRest()
    gh.branch = "commit-existing"
    gh.commit_trees["commit-existing"] = "tree-existing"
    gh.conflicts = 1
    job, spec, outcome = _job(tmp_path)
    await qare_publish.publish(job, spec, outcome, ACCESS, transport=gh.transport)
    assert gh.calls.count(("PATCH", "/repos/acme/widgets/git/refs/heads/qa-assets")) == 2
    assert f"qare/{RID}/result.json" in gh.files


@pytest.mark.asyncio
async def test_github_refusing_is_retryable(tmp_path: Path) -> None:
    job, spec, outcome = _job(tmp_path)
    refusing = httpx.MockTransport(lambda request: httpx.Response(403, json={}))
    with pytest.raises(TransportError, match="403"):
        await qare_publish.publish(job, spec, outcome, ACCESS, transport=refusing)
    assert not (job / qare_publish.PUBLISHED_FILE).exists()


def test_a_symlink_in_the_evidence_is_not_uploaded(tmp_path: Path) -> None:
    job, _spec, outcome = _job(tmp_path)
    link = job / "evidence" / "checks" / "ac-1" / "1" / "link.txt"
    link.symlink_to(tmp_path / "outside.txt")
    outcome.criteria[0].evidence.append("checks/ac-1/1/link.txt")
    files, skipped = qare_publish.collect_files(job, outcome)
    assert "evidence/checks/ac-1/1/link.txt" in skipped
    assert "evidence/checks/ac-1/1/link.txt" not in files


@pytest.mark.parametrize("ref", [".", "./", "", "C:/x", "//host/share/x"])
def test_odd_evidence_paths_are_left_out_not_fatal(tmp_path: Path, ref: str) -> None:
    job, _spec, outcome = _job(tmp_path)
    outcome.criteria[0].evidence.append(ref)
    files, _skipped = qare_publish.collect_files(job, outcome)
    assert "evidence/." not in files
    assert all(name.startswith(("evidence/checks/", "result.json", "plan.json")) for name in files)


def test_free_text_is_redacted_before_it_is_posted(tmp_path: Path) -> None:
    _job_dir, spec, outcome = _job(tmp_path)
    outcome.criteria[0].reason = f"login failed with {SECRET}"
    outcome = outcome.model_copy(update={"summary": f"QAre saw {SECRET}"})
    body = qare_publish.render_comment(spec, outcome, {"prefix": "p", "tree_url": "u", "skipped": []})
    assert SECRET not in body
