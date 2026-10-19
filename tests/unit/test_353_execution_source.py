"""Execution source must retain the tree, not just model review inputs."""

from __future__ import annotations

import hashlib
import importlib
import io
import json
import os
import tarfile

import pytest

COMMIT = "a" * 40
FILES = {
    "Buildfile": (b"build src/main\n", "100644"),
    "src/main": (b"app\n", "100644"),
    "asset.bin": (b"\x00\xff\x80", "100644"),
    "bin/tool": (b"#!/bin/sh\nexit 0\n", "100755"),
    "current": (b"src/main", "120000"),
}


def entries_for(files, algorithm="sha1"):
    return [
        {
            "path": p,
            "mode": mode,
            "size": len(data),
            "object_id": hashlib.new(
                algorithm,
                b"blob " + str(len(data)).encode() + b"\0" + data,
            ).hexdigest(),
        }
        for p, (data, mode) in sorted(files.items())
    ]


def bundle_file(
    tmp_path,
    files=None,
    *,
    entries=None,
    extra=None,
    omit=None,
    commit=COMMIT,
    metadata_change=None,
):
    files = FILES if files is None else files
    entries = entries_for(files) if entries is None else entries
    tree_digest = hashlib.sha256(
        json.dumps(entries, sort_keys=True, separators=(",", ":")).encode(),
    ).hexdigest()
    identity = {
        "schema_version": 1,
        "source_commit": commit,
        "tree_digest": tree_digest,
        "file_count": len(entries),
        "expanded_bytes": sum(x["size"] for x in entries),
    }
    metadata = {"identity": identity, "entries": entries}
    if metadata_change:
        metadata_change(metadata)
    output = tmp_path / "bundle.tgz"
    with tarfile.open(output, "w:gz") as archive:
        raw = json.dumps(metadata, sort_keys=True, separators=(",", ":")).encode()
        info = tarfile.TarInfo("metadata.json")
        info.size = len(raw)
        archive.addfile(info, io.BytesIO(raw))
        for p, (data, mode) in files.items():
            if p == omit:
                continue
            info = tarfile.TarInfo("files/" + p)
            if mode == "120000":
                info.type = tarfile.SYMTYPE
                info.linkname = data.decode()
                archive.addfile(info)
            else:
                info.size = len(data)
                info.mode = 0o755 if mode == "100755" else 0o644
                archive.addfile(info, io.BytesIO(data))
        if extra:
            info, data = extra
            archive.addfile(info, io.BytesIO(data) if data else None)
    return output, identity


def source_api():
    # An explicit assertion makes missing functionality a failing test, not collection error.
    assert (
        importlib.util.find_spec("coordinare_service_inference.environment.execution_source")
        is not None
    )
    return importlib.import_module("coordinare_service_inference.environment.execution_source")


def test_complete_tree_preserves_unreviewed_binary_executable_and_link(tmp_path):
    api = source_api()
    restore = importlib.import_module(
        "coordinare_service_inference.environment.execution_source_restore",
    )
    path, identity = bundle_file(tmp_path)
    expected = api.ExecutionSourceIdentity.model_validate(identity)
    review = {"Buildfile": hashlib.sha256(FILES["Buildfile"][0]).hexdigest()}
    assert api.validate_execution_bundle(path, expected=expected, review_inputs=review) == expected
    restore.restore_execution_bundle(
        path,
        tmp_path / "repo",
        expected=expected,
        review_inputs=review,
    )
    assert (tmp_path / "repo/src/main").read_bytes() == b"app\n"
    assert (tmp_path / "repo/asset.bin").read_bytes() == b"\x00\xff\x80"
    assert (tmp_path / "repo/bin/tool").stat().st_mode & 0o111 == 0o111
    assert os.readlink(tmp_path / "repo/current") == "src/main"
    assert (tmp_path / "repo/current").read_bytes() == b"app\n"
    with pytest.raises(ValueError):
        restore.restore_execution_bundle(
            path,
            tmp_path / "repo",
            expected=expected,
            review_inputs=review,
        )


@pytest.mark.parametrize(
    "change",
    ["missing", "extra", "duplicate", "wrong_hash", "review", "identity"],
)
def test_incomplete_or_corrupt_source_refused(tmp_path, change):
    api = source_api()
    entries = entries_for(FILES)
    if change == "wrong_hash":
        entries[0]["object_id"] = "0" * 40
    extra = None
    if change in {"extra", "duplicate"}:
        info = tarfile.TarInfo("files/Buildfile" if change == "duplicate" else "files/extra")
        info.size = 1
        extra = (info, b"x")
    path, identity = bundle_file(
        tmp_path,
        entries=entries,
        extra=extra,
        omit="src/main" if change == "missing" else None,
    )
    if change == "identity":
        identity["source_commit"] = "b" * 40
    with pytest.raises(ValueError):
        api.validate_execution_bundle(
            path,
            expected=api.ExecutionSourceIdentity.model_validate(identity),
            review_inputs={"Buildfile": "0" * 64} if change == "review" else None,
        )


@pytest.mark.parametrize(
    "name,kind,target",
    [
        ("../outside", "file", ""),
        ("/absolute", "file", ""),
        ("evil", "symlink", "../../outside"),
        ("evil", "symlink", "/outside"),
        ("evil", "hardlink", "src/main"),
        ("evil", "fifo", ""),
        ("evil", "device", ""),
    ],
)
def test_unsafe_archive_never_writes_outside_owned_root(tmp_path, name, kind, target):
    api = source_api()
    (tmp_path / "outside").write_text("sentinel")
    info = tarfile.TarInfo("files/" + name)
    info.size = 0
    info.type = {
        "file": tarfile.REGTYPE,
        "symlink": tarfile.SYMTYPE,
        "hardlink": tarfile.LNKTYPE,
        "fifo": tarfile.FIFOTYPE,
        "device": tarfile.CHRTYPE,
    }[kind]
    info.linkname = target
    path, _ = bundle_file(tmp_path, extra=(info, None))
    with pytest.raises(ValueError):
        api.validate_execution_bundle(path)
    assert (tmp_path / "outside").read_text() == "sentinel"


@pytest.mark.parametrize(
    "files",
    [
        {"a": (b"b", "120000"), "b": (b"a", "120000")},
        {"a": (b"missing", "120000")},
        {"a": (b"x", "100644"), "a/child": (b"y", "100644")},
        {"a": (b"version https://git-lfs.github.com/spec/v1\noid sha256:000\nsize 10\n", "100644")},
    ],
)
def test_unsupported_or_inconsistent_tree_holds(tmp_path, files):
    api = source_api()
    path, _ = bundle_file(tmp_path, files)
    with pytest.raises(ValueError):
        api.validate_execution_bundle(path)


@pytest.mark.parametrize(
    "constant,limit",
    [
        ("MAX_ARCHIVE_BYTES", 1),
        ("MAX_EXPANDED_BYTES", 1),
        ("MAX_BLOB_BYTES", 1),
        ("MAX_ENTRIES", 1),
        ("MAX_METADATA_BYTES", 1),
    ],
)
def test_source_resource_bounds(tmp_path, monkeypatch, constant, limit):
    api = source_api()
    path, _ = bundle_file(tmp_path)
    monkeypatch.setattr(api, constant, limit)
    with pytest.raises(ValueError):
        api.validate_execution_bundle(path)


def test_identity_stable_across_order_and_compression(tmp_path):
    api = source_api()
    p, _ = bundle_file(tmp_path)
    first = api.validate_execution_bundle(p)
    p, _ = bundle_file(tmp_path, dict(reversed(list(FILES.items()))))
    assert api.validate_execution_bundle(p).digest() == first.digest()
    p, identity = bundle_file(tmp_path, entries=entries_for(FILES, "sha256"), commit="a" * 64)
    assert api.validate_execution_bundle(p).source_commit == "a" * 64
    with pytest.raises(ValueError):
        api.ExecutionSourceIdentity.model_validate({**identity, "source_commit": "a" * 41})


def test_projected_bundle_cli_validates_chunks_before_restoration(tmp_path):
    import subprocess
    import sys

    api = source_api()
    path, identity = bundle_file(tmp_path)
    raw = path.read_bytes()
    payload = tmp_path / "projected"
    payload.mkdir()
    (payload / "chunk-0000").write_bytes(raw)
    (payload / "transport.json").write_text(
        json.dumps(
            {
                "sha256": hashlib.sha256(raw).hexdigest(),
                "size_bytes": len(raw),
                "count": 1,
                "identity": identity,
                "review_inputs": {},
            },
        ),
    )
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-m",
            "coordinare_service_inference.environment.execution_source_restore",
            str(payload),
            str(tmp_path / "restored"),
        ],
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr.decode()
    assert (tmp_path / "restored/src/main").read_bytes() == b"app\n"
    (payload / "chunk-0000").write_bytes(b"corrupt")
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-m",
            "coordinare_service_inference.environment.execution_source_restore",
            str(payload),
            str(tmp_path / "bad"),
        ],
        capture_output=True,
    )
    assert result.returncode != 0
    assert not (tmp_path / "bad").exists()
    assert api.validate_execution_bundle(path).digest()


def test_source_bounds_have_source_specific_failure_code(tmp_path, monkeypatch):
    api = source_api()
    path, _ = bundle_file(tmp_path)
    monkeypatch.setattr(api, "MAX_ARCHIVE_BYTES", 1)
    with pytest.raises(ValueError) as failure:
        api.validate_execution_bundle(path)
    assert getattr(failure.value, "code", None) == "execution_source_limit"


def test_unsupported_hydration_has_source_specific_failure_code(tmp_path):
    api = source_api()
    path, _ = bundle_file(
        tmp_path,
        {
            "pointer": (
                b"version https://git-lfs.github.com/spec/v1\noid sha256:000\nsize 10\n",
                "100644",
            ),
        },
    )
    with pytest.raises(ValueError) as failure:
        api.validate_execution_bundle(path)
    assert getattr(failure.value, "code", None) == "execution_source_unsupported"


def test_intermediate_symlink_parent_steps_cannot_escape_owned_root(tmp_path):
    api = source_api()
    files = {
        'deep/link': (b'../x', '120000'),
        'x/leaf': (b'inside', '100644'),
        'a': (b'deep/link/../../escape', '120000'),
        'escape': (b'inside', '100644'),
    }
    (tmp_path/'escape').write_text('external secret')
    path, _ = bundle_file(tmp_path, files)
    with pytest.raises(ValueError, match=r'escape|outside|relative'):
        api.validate_execution_bundle(path, review_inputs={
            'a': hashlib.sha256(b'inside').hexdigest(),
        })


def test_restored_tree_has_git_checkout_permissions_under_private_umask(tmp_path):
    from coordinare_service_inference.environment.execution_source_restore import (
        restore_execution_bundle,
    )

    path, identity = bundle_file(tmp_path)
    expected = source_api().ExecutionSourceIdentity.model_validate(identity)
    private_parent = tmp_path / 'private'
    private_parent.mkdir(mode=0o700)
    previous = os.umask(0o077)
    try:
        restore_execution_bundle(path, private_parent / 'repo', expected=expected, review_inputs={})
    finally:
        os.umask(previous)
    assert private_parent.stat().st_mode & 0o777 == 0o700
    for name in ('repo', 'repo/src', 'repo/bin'):
        assert (private_parent / name).stat().st_mode & 0o777 == 0o755
    assert (private_parent / 'repo/src/main').stat().st_mode & 0o777 == 0o644
    assert (private_parent / 'repo/bin/tool').stat().st_mode & 0o777 == 0o755
