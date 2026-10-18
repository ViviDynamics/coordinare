"""Provider boundaries use complete immutable trees and bounded binary content."""

from __future__ import annotations

import importlib
import io
import tarfile
import time

import httpx
import pytest

from tests.unit.test_353_execution_source import COMMIT, FILES, entries_for


def api():
    assert importlib.util.find_spec("coordinare.services.environment_source_provider") is not None
    return importlib.import_module("coordinare.services.environment_source_provider")


def provider_archive(files):
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w:gz") as archive:
        directory = tarfile.TarInfo("owner-project-abc/")
        directory.type = tarfile.DIRTYPE
        archive.addfile(directory)
        for name, (data, mode) in files.items():
            entry = tarfile.TarInfo("owner-project-abc/" + name)
            entry.mode = 0o755 if mode == "100755" else 0o644
            if mode == "120000":
                entry.type = tarfile.SYMTYPE
                entry.linkname = data.decode()
                archive.addfile(entry)
            else:
                entry.size = len(data)
                archive.addfile(entry, io.BytesIO(data))
    return raw.getvalue()


async def prepare(
    tmp_path,
    monkeypatch,
    *,
    omit=None,
    substitute=False,
    tree_change=None,
    redirect=None,
):
    module = api()
    calls = []
    entries = entries_for(FILES)
    tree = {
        "truncated": False,
        "tree": [
            {
                "path": e["path"],
                "mode": e["mode"],
                "type": "blob",
                "sha": e["object_id"],
                "size": e["size"],
            }
            for e in entries
        ],
    }
    if tree_change:
        tree_change(tree)
    selected = {k: v for k, v in FILES.items() if k != omit}
    if substitute:
        selected["Buildfile"] = (b"wrong", "100644")

    def answer(request):
        calls.append(request)
        if request.url.host == "downloads.example":
            assert "authorization" not in request.headers
            return httpx.Response(200, content=provider_archive(selected))
        assert request.headers["authorization"] == "Bearer private-test-token"
        if "/git/trees/" in request.url.path:
            return httpx.Response(200, json=tree)
        if "/tarball/" in request.url.path:
            if redirect:
                return httpx.Response(302, headers={"location": redirect})
            return httpx.Response(200, content=provider_archive(selected))
        if "/git/blobs/" in request.url.path:
            oid = request.url.path.rsplit("/", 1)[-1]
            entry = next(e for e in entries if e["object_id"] == oid)
            return httpx.Response(200, content=FILES[entry["path"]][0])
        return httpx.Response(404)

    original = httpx.AsyncClient
    monkeypatch.setattr(
        module.httpx,
        "AsyncClient",
        lambda **kw: original(transport=httpx.MockTransport(answer), **kw),
    )
    result = await module.acquire_github_source(
        module.GitHubSourceRequest(
            "https://api.github.com", "private-test-token", "owner", "project", COMMIT,
        ),
        destination=tmp_path / "owned",
        deadline=time.monotonic() + 10,
    )
    return result, calls


@pytest.mark.asyncio
async def test_archive_acquisition_uses_one_tree_and_one_archive_for_complete_source(
    tmp_path,
    monkeypatch,
):
    bundle, calls = await prepare(tmp_path, monkeypatch)
    assert len(calls) == 2
    assert bundle.identity.file_count == 5
    assert bundle.payload_path.is_file()
    assert bundle.payload_bytes == bundle.payload_path.stat().st_size


@pytest.mark.asyncio
async def test_export_ignored_blobs_are_completed_from_pinned_objects(tmp_path, monkeypatch):
    bundle, calls = await prepare(tmp_path, monkeypatch, omit="src/main")
    assert len(calls) == 3
    assert "/git/blobs/" in calls[-1].url.path
    from coordinare_service_inference.environment.execution_source import validate_execution_bundle

    assert validate_execution_bundle(bundle.payload_path) == bundle.identity


@pytest.mark.asyncio
async def test_substituted_archive_content_is_not_silently_replaced(tmp_path, monkeypatch):
    with pytest.raises(ValueError):
        await prepare(tmp_path, monkeypatch, substitute=True)
    assert not (tmp_path / "owned").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        lambda t: t.update(truncated=True),
        lambda t: t["tree"].append(t["tree"][0]),
        lambda t: t["tree"][0].update(mode="160000", type="commit"),
        lambda t: t["tree"][0].update(sha="main"),
        lambda t: t["tree"][0].update(path="../escape"),
    ],
)
async def test_invalid_provider_tree_holds(tmp_path, monkeypatch, change):
    with pytest.raises(ValueError):
        await prepare(tmp_path, monkeypatch, tree_change=change)
    assert not (tmp_path / "owned").exists()


@pytest.mark.asyncio
async def test_signed_archive_redirect_never_receives_github_token(tmp_path, monkeypatch):
    _, calls = await prepare(
        tmp_path,
        monkeypatch,
        redirect="https://downloads.example/source.tgz?signature=private",
    )
    assert len(calls) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    ["http://downloads.example/source", "https://user:pass@downloads.example/source"],
)
async def test_unsafe_redirects_hold(tmp_path, monkeypatch, url):
    with pytest.raises(ValueError):
        await prepare(tmp_path, monkeypatch, redirect=url)


@pytest.mark.asyncio
async def test_expired_acquisition_and_mutable_commit_never_fetch(tmp_path):
    module = api()
    with pytest.raises(ValueError):
        await module.acquire_github_source(
            module.GitHubSourceRequest("https://api.github.com", "t", "o", "r", "main"),
            destination=tmp_path / "owned",
            deadline=time.monotonic() + 10,
        )
    with pytest.raises(TimeoutError):
        await module.acquire_github_source(
            module.GitHubSourceRequest("https://api.github.com", "t", "o", "r", COMMIT),
            destination=tmp_path / "owned",
            deadline=time.monotonic() - 1,
        )
    assert not (tmp_path / "owned").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "limit_name,limit",
    [
        ("MAX_ENTRIES", 1),
        ("MAX_ARCHIVE_BYTES", 1),
        ("MAX_METADATA_BYTES", 1),
        ("MAX_FALLBACK_BLOBS", 0),
    ],
)
async def test_provider_resource_bounds_cleanup_partial_acquisition(
    tmp_path,
    monkeypatch,
    limit_name,
    limit,
):
    module = api()
    monkeypatch.setattr(module, limit_name, limit)
    with pytest.raises(ValueError):
        await prepare(
            tmp_path,
            monkeypatch,
            omit="src/main" if limit_name == "MAX_FALLBACK_BLOBS" else None,
        )
    assert not (tmp_path / "owned").exists()


@pytest.mark.asyncio
async def test_github_service_exposes_production_provider(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from coordinare.services.github import GitHubService

    async def token():
        return "secret"

    fake = SimpleNamespace(_current_token=token, _rest_api_base=lambda: "https://api.github.com")
    captured = {}

    async def acquire(request, **kwargs):
        captured.update(
            base=request.api_base,
            auth=request.token,
            owner=request.owner,
            repo=request.repo,
            commit=request.commit,
            **kwargs,
        )
        return "bundle"

    monkeypatch.setattr("coordinare.services.github.acquire_github_source", acquire)
    assert hasattr(GitHubService, "prepare_execution_source")
    result = await GitHubService.prepare_execution_source(
        fake,
        "o",
        "r",
        commit=COMMIT,
        destination=tmp_path / "owned",
        deadline=123,
    )
    assert result == "bundle"
    assert captured["commit"] == COMMIT and captured["auth"] == "secret"


@pytest.mark.asyncio
async def test_cancelled_acquisition_removes_owned_temporary_source(tmp_path, monkeypatch):
    import asyncio

    module = api()
    entered = asyncio.Event()

    async def answer(request):
        entered.set()
        await asyncio.Event().wait()

    original = httpx.AsyncClient
    monkeypatch.setattr(
        module.httpx,
        "AsyncClient",
        lambda **kw: original(transport=httpx.MockTransport(answer), **kw),
    )
    task = asyncio.create_task(
        module.acquire_github_source(
            module.GitHubSourceRequest("https://api.github.com", "t", "o", "r", COMMIT),
            destination=tmp_path / "owned",
            deadline=time.monotonic() + 30,
        ),
    )
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not (tmp_path / "owned").exists()


@pytest.mark.asyncio
async def test_private_archive_signature_not_in_http_request_logs(tmp_path, monkeypatch, caplog):
    import logging

    caplog.set_level(logging.INFO, logger="httpx")
    await prepare(
        tmp_path,
        monkeypatch,
        redirect="https://downloads.example/source.tgz?token=private-signature",
    )
    assert "private-signature" not in caplog.text


@pytest.mark.asyncio
async def test_github_review_reads_resolve_pinned_in_tree_links_like_snapshot(
    tmp_path,
    monkeypatch,
):
    from types import SimpleNamespace

    from coordinare.services.github import GitHubService

    async def token():
        return "test"

    seen = []

    async def query(document, variables):
        seen.append(variables["expression"])
        text = {"link": "docs/guide", "docs/guide": "required tool"}[
            variables["expression"].split(":", 1)[1]
        ]
        return {"repository": {"object": {"text": text}}}

    fake = SimpleNamespace(
        _current_token=token,
        _rest_api_base=lambda: "https://api.github.com",
        _guarded_execute=query,
    )

    def answer(request):
        return httpx.Response(
            200,
            json={
                "truncated": False,
                "tree": [
                    {"path": "link", "type": "blob", "mode": "120000"},
                    {"path": "docs/guide", "type": "blob", "mode": "100644"},
                ],
            },
        )

    original = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kw: original(transport=httpx.MockTransport(answer), **kw),
    )
    await GitHubService.get_repository_paths(fake, "owner", "repo", ref=COMMIT)
    assert (
        await GitHubService.get_file_content(fake, "owner", "repo", "link", ref=COMMIT)
        == "required tool"
    )
    assert seen == [COMMIT + ":link", COMMIT + ":docs/guide"]
