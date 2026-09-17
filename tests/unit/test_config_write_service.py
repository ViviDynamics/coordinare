"""Foundational unit tests for the config write service (spec 081-config-ui).

Covers T010 write-service concerns: content-hash determinism (research D4),
concurrency-guard rejection (research D4), and atomic write-back with mode
preservation (research D5).
"""

from __future__ import annotations

import os
import stat
import tempfile
from pathlib import Path

import pytest
import yaml

from coordinare.services import config_write_service as cws

# --- T008: content-hash determinism + concurrency guard -----------------------


def test_compute_content_hash_is_deterministic(tmp_path: Path):
    p = tmp_path / "config.yaml"
    p.write_text("a: 1\n")
    h1 = cws.compute_content_hash(p)
    h2 = cws.compute_content_hash(p)
    assert h1 == h2
    assert h1.startswith("sha256:")


def test_compute_content_hash_changes_with_content(tmp_path: Path):
    p = tmp_path / "config.yaml"
    p.write_text("a: 1\n")
    h1 = cws.compute_content_hash(p)
    p.write_text("a: 2\n")
    h2 = cws.compute_content_hash(p)
    assert h1 != h2


def test_compute_content_hash_missing_file_is_stable(tmp_path: Path):
    p = tmp_path / "absent.yaml"
    h1 = cws.compute_content_hash(p)
    h2 = cws.compute_content_hash(p)
    assert h1 == h2
    assert h1.startswith("sha256:")


def test_compute_content_hash_non_file_is_missing_sentinel(tmp_path: Path):
    """A path that exists but is not a regular file (e.g. a directory due to a
    mount glitch) must hash to the same empty/missing sentinel as an absent file
    rather than raising OSError from ``read_bytes`` — hashing is total (Copilot
    round 14)."""
    d = tmp_path / "config.yaml"
    d.mkdir()
    assert d.exists() and not d.is_file()
    absent = tmp_path / "absent.yaml"
    assert cws.compute_content_hash(d) == cws.compute_content_hash(absent)


def test_compute_content_hash_unreadable_file_is_missing_sentinel(
    tmp_path: Path, monkeypatch,
):
    """compute_content_hash promises totality: if the path is_file() but
    read_bytes raises OSError (permission/mount issue), it returns the same
    empty/missing sentinel rather than propagating (Copilot round 15)."""
    p = tmp_path / "config.yaml"
    p.write_text("x: 1\n", encoding="utf-8")

    def _boom(self, *a, **k):
        raise OSError("permission denied")

    monkeypatch.setattr(Path, "read_bytes", _boom)
    absent = tmp_path / "absent.yaml"
    assert cws.compute_content_hash(p) == cws.compute_content_hash(absent)


def test_atomic_write_yaml_closes_fd_when_fdopen_fails(tmp_path: Path, monkeypatch):
    """If os.fdopen raises after mkstemp (e.g. resource exhaustion), the raw fd
    must be closed and the temp file unlinked rather than leaked (Copilot
    round 15)."""
    p = tmp_path / "config.yaml"
    captured: dict[str, int] = {}
    closed: list[int] = []
    real_mkstemp = tempfile.mkstemp
    real_close = os.close

    def _spy_mkstemp(*a, **k):
        fd, name = real_mkstemp(*a, **k)
        captured["fd"] = fd
        return fd, name

    def _boom_fdopen(*a, **k):
        raise OSError("too many open files")

    def _spy_close(fd):
        closed.append(fd)
        return real_close(fd)

    monkeypatch.setattr(cws.tempfile, "mkstemp", _spy_mkstemp)
    monkeypatch.setattr(cws.os, "fdopen", _boom_fdopen)
    monkeypatch.setattr(cws.os, "close", _spy_close)

    with pytest.raises(OSError):
        cws.atomic_write_yaml(p, {"a": 1})

    assert captured["fd"] in closed, "leaked fd was never closed"
    assert not list(tmp_path.glob(".config.yaml.*.tmp")), "temp file leaked"


def test_guard_concurrency_passes_when_hash_matches(tmp_path: Path):
    p = tmp_path / "config.yaml"
    p.write_text("a: 1\n")
    current = cws.compute_content_hash(p)
    # Should not raise.
    cws.guard_concurrency(p, current)


def test_guard_concurrency_rejects_on_mismatch(tmp_path: Path):
    p = tmp_path / "config.yaml"
    p.write_text("a: 1\n")
    stale = cws.compute_content_hash(p)
    p.write_text("a: 999\n")  # someone else wrote
    with pytest.raises(cws.ConcurrencyConflictError):
        cws.guard_concurrency(p, stale)


def test_guard_concurrency_raises_oserror_for_unreadable_present_file(
    tmp_path: Path, monkeypatch,
):
    """An unreadable-but-present regular file must NOT be silently treated as
    "empty" by the guard: compute_content_hash() returns the empty sentinel on
    OSError, so a sentinel base_hash would otherwise spuriously match and let a
    write clobber a file the UI could not read. The guard must re-probe and
    re-raise OSError so callers degrade to a structured ``forbidden`` result."""
    p = tmp_path / "config.yaml"
    p.write_text("a: 1\n")
    empty_sentinel = "sha256:" + __import__("hashlib").sha256(b"").hexdigest()

    real_read_bytes = Path.read_bytes

    def boom_read_bytes(self):
        if self == p:
            raise PermissionError("unreadable mount")
        return real_read_bytes(self)

    monkeypatch.setattr(Path, "read_bytes", boom_read_bytes)
    # base_hash equals the empty sentinel (the only value an unreadable file can
    # produce), so a hash-only comparison would pass. The guard must instead raise.
    with pytest.raises(OSError):
        cws.guard_concurrency(p, empty_sentinel)


# --- T009: atomic write-back --------------------------------------------------


def test_atomic_write_yaml_round_trips(tmp_path: Path):
    p = tmp_path / "config.yaml"
    p.write_text("a: 1\n")
    new_hash = cws.atomic_write_yaml(p, {"a": 2, "b": ["x", "y"]})
    assert yaml.safe_load(p.read_text()) == {"a": 2, "b": ["x", "y"]}
    assert new_hash == cws.compute_content_hash(p)


def test_atomic_write_yaml_preserves_mode(tmp_path: Path):
    p = tmp_path / "config.yaml"
    p.write_text("a: 1\n")
    os.chmod(p, 0o600)
    cws.atomic_write_yaml(p, {"a": 2})
    mode = stat.S_IMODE(os.stat(p).st_mode)
    assert mode == 0o600


def test_atomic_write_yaml_chmods_permission_bits_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    """The mode handed to ``os.chmod`` carries only permission bits.

    ``os.stat().st_mode`` includes the file-type bits (e.g. ``S_IFREG``); passing
    them through to ``os.chmod`` is non-portable. The write must mask with
    ``stat.S_IMODE`` so only the rwx/setuid/sticky permission bits are applied.
    """
    p = tmp_path / "config.yaml"
    p.write_text("a: 1\n")
    os.chmod(p, 0o640)

    captured: list[int] = []
    real_chmod = cws.os.chmod

    def _spy_chmod(target, mode, *args, **kwargs):
        captured.append(mode)
        return real_chmod(target, mode, *args, **kwargs)

    monkeypatch.setattr(cws.os, "chmod", _spy_chmod)
    cws.atomic_write_yaml(p, {"a": 2})

    assert captured, "os.chmod was not called"
    for mode in captured:
        # No file-type bits leaked through — permission bits only.
        assert mode == stat.S_IMODE(mode)
    assert captured[0] == 0o640


def test_atomic_write_yaml_creates_new_file(tmp_path: Path):
    p = tmp_path / "new.yaml"
    cws.atomic_write_yaml(p, {"created": True})
    assert p.exists()
    assert yaml.safe_load(p.read_text()) == {"created": True}


def test_atomic_write_yaml_leaves_no_temp_files(tmp_path: Path):
    p = tmp_path / "config.yaml"
    p.write_text("a: 1\n")
    cws.atomic_write_yaml(p, {"a": 2})
    leftovers = [
        f for f in tmp_path.iterdir() if f.name != "config.yaml"
    ]
    assert leftovers == []


# --- T050: fault-injection atomicity (SC-007) ---------------------------------


def test_atomic_write_yaml_crash_before_replace_leaves_original_intact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    """Simulate a crash between the tempfile write and ``os.replace``.

    Patching ``os.replace`` to raise proves the write is all-or-nothing (SC-007):
    the original ``config.yaml`` must be byte-for-byte intact (no truncation, no
    partial write) and no temp file may be left adjacent.
    """
    p = tmp_path / "config.yaml"
    original = "a: 1\nb:\n  - x\n  - y\n"
    p.write_text(original)
    original_bytes = p.read_bytes()

    def _boom(*_args, **_kwargs):
        raise OSError("simulated crash during replace")

    monkeypatch.setattr(cws.os, "replace", _boom)

    with pytest.raises(OSError, match="simulated crash"):
        cws.atomic_write_yaml(p, {"a": 999, "b": ["clobbered"]})

    # Original file untouched, byte-for-byte.
    assert p.read_bytes() == original_bytes
    # No leftover temp file adjacent to the target.
    leftovers = [f for f in tmp_path.iterdir() if f.name != "config.yaml"]
    assert leftovers == []


# --- T022: hot-reload vs restart classification (research D7) ------------------


def test_classify_applied_hot_reloaded_for_tunable_field():
    assert cws.classify_section_applied("global", ["poll_interval_seconds"]) == "hot_reloaded"


def test_classify_applied_staged_restart_for_process_binding():
    # dashboard_port is a process binding read once at startup (restart_required).
    assert cws.classify_section_applied("global", ["dashboard_port"]) == "staged_restart"


def test_classify_applied_staged_restart_when_any_field_requires_restart():
    applied = cws.classify_section_applied(
        "global", ["poll_interval_seconds", "dashboard_host"],
    )
    assert applied == "staged_restart"


# --- T022: section save round-trip + masked-secret no-op ----------------------


def test_save_section_persists_and_classifies(temp_config_path: Path):
    base_hash = cws.compute_content_hash(temp_config_path)
    req = cws.SaveRequest(
        section="global",
        changes={"poll_interval_seconds": 45},
        base_hash=base_hash,
    )
    result = cws.save_section(req, temp_config_path)
    assert result.ok is True
    assert result.applied == "hot_reloaded"
    assert yaml.safe_load(temp_config_path.read_text())["poll_interval_seconds"] == 45


def test_save_section_noop_does_not_rewrite_file(temp_config_path: Path, monkeypatch):
    """A save whose candidate equals what was read is a success no-op.

    Submitting a change that sets a field to its current value (or a payload
    that reduces to no change after the masked-secret skip) must NOT rewrite
    config.yaml — rewriting needlessly strips comments (``safe_dump``) and can
    trigger a reload. The result is ``ok`` with ``new_hash`` equal to the
    unchanged on-disk hash, and the atomic writer is never invoked.
    """

    def _fail_write(*args, **kwargs):  # pragma: no cover - must not run
        raise AssertionError("no-op save must not rewrite config.yaml")

    monkeypatch.setattr(cws, "atomic_write_yaml", _fail_write)

    base_hash = cws.compute_content_hash(temp_config_path)
    req = cws.SaveRequest(
        section="global",
        changes={"poll_interval_seconds": 30},  # already 30 on disk → no-op
        base_hash=base_hash,
    )
    result = cws.save_section(req, temp_config_path)
    assert result.ok is True
    assert result.new_hash == base_hash


def test_save_section_empty_changes_is_noop(temp_config_path: Path, monkeypatch):
    """An empty ``changes`` payload reduces to a no-op and never rewrites."""

    def _fail_write(*args, **kwargs):  # pragma: no cover - must not run
        raise AssertionError("empty-changes save must not rewrite config.yaml")

    monkeypatch.setattr(cws, "atomic_write_yaml", _fail_write)

    base_hash = cws.compute_content_hash(temp_config_path)
    req = cws.SaveRequest(section="global", changes={}, base_hash=base_hash)
    result = cws.save_section(req, temp_config_path)
    assert result.ok is True
    assert result.new_hash == base_hash


def test_save_section_conflict_on_stale_hash(temp_config_path: Path):
    req = cws.SaveRequest(
        section="global",
        changes={"poll_interval_seconds": 45},
        base_hash="sha256:deadbeef",
    )
    result = cws.save_section(req, temp_config_path)
    assert result.ok is False
    assert result.errors[0].code == "conflict"


def test_save_section_missing_base_hash_is_validation_not_conflict(
    temp_config_path: Path,
):
    """An empty/missing base_hash is a client error, not a stale-file conflict.

    Without this guard an empty base_hash never matches the real content hash, so
    the operator would see a misleading "config changed on disk" conflict instead
    of a clear "base_hash required" validation error.
    """
    req = cws.SaveRequest(
        section="global",
        changes={"poll_interval_seconds": 45},
        base_hash="",
    )
    result = cws.save_section(req, temp_config_path)
    assert result.ok is False
    assert result.errors[0].code == "validation"
    assert "base_hash" in result.errors[0].message


def test_save_section_rechecks_conflict_just_before_write(
    temp_config_path: Path, monkeypatch,
):
    """FR-016: the optimistic-concurrency guard runs immediately before the swap.

    A concurrent out-of-band edit that lands *after* the initial read/validate but
    *before* the atomic ``os.replace`` must still be rejected as a conflict — never
    a last-writer-wins clobber. We simulate that race by mutating the file on disk
    inside ``_validate_candidate`` (which runs between read and write); if the guard
    only ran early this save would succeed and clobber the concurrent edit.
    """
    base_hash = cws.compute_content_hash(temp_config_path)
    real_validate = cws._validate_candidate

    def mutate_then_validate(candidate):
        temp_config_path.write_text(
            temp_config_path.read_text() + "\n# concurrent out-of-band edit\n",
        )
        return real_validate(candidate)

    monkeypatch.setattr(cws, "_validate_candidate", mutate_then_validate)

    req = cws.SaveRequest(
        section="global",
        changes={"poll_interval_seconds": 45},
        base_hash=base_hash,
    )
    result = cws.save_section(req, temp_config_path)
    assert result.ok is False
    assert result.errors[0].code == "conflict"


def test_create_catalog_item_rechecks_conflict_just_before_write(
    temp_config_path: Path, monkeypatch,
):
    """FR-016 for the catalog CRUD path: the guard runs just before the swap.

    Mirrors ``test_save_section_rechecks_conflict_just_before_write`` for
    ``create_catalog_item`` — a concurrent edit landing after read/validate must be
    rejected as a conflict, not clobbered.
    """
    base_hash = cws.compute_content_hash(temp_config_path)
    real_validate = cws._validate_candidate

    def mutate_then_validate(candidate):
        temp_config_path.write_text(
            temp_config_path.read_text() + "\n# concurrent out-of-band edit\n",
        )
        return real_validate(candidate)

    monkeypatch.setattr(cws, "_validate_candidate", mutate_then_validate)

    result = cws.create_catalog_item(
        "endpoints",
        {"name": "brand-new-endpoint", "kind": "vllm", "base_url": "http://localhost:9"},
        base_hash,
        temp_config_path,
    )
    assert result.ok is False
    assert result.errors[0].code == "conflict"


def test_save_section_rejects_unsupported_section(temp_config_path: Path):
    """``save_section`` only writes the ``global`` scalar group; other sections
    (catalogs, personas) have their own endpoints and must be rejected defensively
    rather than silently writing arbitrary top-level keys into config.yaml."""
    base_hash = cws.compute_content_hash(temp_config_path)
    req = cws.SaveRequest(
        section="endpoints",
        changes={"anything": 1},
        base_hash=base_hash,
    )
    result = cws.save_section(req, temp_config_path)
    assert result.ok is False
    assert result.errors[0].code == "forbidden"
    assert "endpoints" in result.errors[0].message


def test_save_section_rejects_non_config_store(temp_config_path: Path):
    """``save_section`` targets ``config.yaml`` only; a routing-store request must
    be rejected rather than written into the config path."""
    base_hash = cws.compute_content_hash(temp_config_path)
    req = cws.SaveRequest(
        store="routing_yaml",
        section="global",
        changes={"poll_interval_seconds": 45},
        base_hash=base_hash,
    )
    result = cws.save_section(req, temp_config_path)
    assert result.ok is False
    assert result.errors[0].code == "forbidden"


def test_save_section_validation_error_is_secret_free(temp_config_path: Path):
    base_hash = cws.compute_content_hash(temp_config_path)
    req = cws.SaveRequest(
        section="global",
        changes={"poll_interval_seconds": 999999},
        base_hash=base_hash,
    )
    result = cws.save_section(req, temp_config_path)
    assert result.ok is False
    assert result.errors[0].code == "validation"
    assert "Traceback" not in result.errors[0].message


def test_save_section_masked_secret_is_noop(temp_config_path: Path):
    from coordinare.config_descriptors import SECRET_MASK

    base_hash = cws.compute_content_hash(temp_config_path)
    req = cws.SaveRequest(
        section="global",
        changes={"github_token": SECRET_MASK},
        base_hash=base_hash,
    )
    result = cws.save_section(req, temp_config_path)
    assert result.ok is True
    # The on-disk ${VAR} placeholder is preserved — the mask is never written.
    assert (
        yaml.safe_load(temp_config_path.read_text())["github_token"]
        == "${COORDINARE_GITHUB_TOKEN}"
    )


# --- T044: no secret leakage (SC-008, quickstart §6) --------------------------


def test_save_masked_secret_never_writes_mask_or_plaintext(temp_config_path: Path):
    """A masked-unchanged secret save is a strict no-op: neither the mask glyphs
    nor any expanded plaintext is ever written to disk.

    Guards SC-008 — the ``••••••`` placeholder the UI shows for a secret must
    round-trip back to the on-disk ``${VAR}`` reference, never overwriting it and
    never substituting an expanded value.
    """
    from coordinare.config_descriptors import SECRET_MASK

    base_hash = cws.compute_content_hash(temp_config_path)
    req = cws.SaveRequest(
        section="global",
        changes={"github_token": SECRET_MASK},
        base_hash=base_hash,
    )
    result = cws.save_section(req, temp_config_path)
    assert result.ok is True

    on_disk = temp_config_path.read_text()
    # The on-disk ${VAR} reference is preserved verbatim.
    assert "${COORDINARE_GITHUB_TOKEN}" in on_disk
    # The mask glyphs are never persisted.
    assert SECRET_MASK not in on_disk
    # No SaveResult field carries the mask or an expanded secret back to the UI.
    assert SECRET_MASK not in (result.message or "")
    for err in result.errors:
        assert SECRET_MASK not in err.message


def test_save_section_rejects_non_scalar_field(temp_config_path: Path):
    """A non-scalar Global field (a catalog/collection like ``endpoints``) must not
    be mutable through the scalar-group section-save endpoint.

    ``ProjectConfiguration`` is ``extra="ignore"``, so without a per-field guard a
    request carrying ``endpoints`` (a catalog with its own CRUD endpoints) would be
    written straight into the top-level mapping, bypassing the per-catalog API
    boundary. Reject it as ``validation`` and write nothing.
    """
    before = temp_config_path.read_text()
    base_hash = cws.compute_content_hash(temp_config_path)
    req = cws.SaveRequest(
        section="global",
        changes={"endpoints": [{"name": "junk"}]},
        base_hash=base_hash,
    )
    result = cws.save_section(req, temp_config_path)
    assert result.ok is False
    assert result.errors[0].code == "validation"
    assert "endpoints" in result.errors[0].message
    assert temp_config_path.read_text() == before


def test_save_section_rejects_unknown_field(temp_config_path: Path):
    """An unknown key must not be silently persisted into config.yaml.

    Because ``ProjectConfiguration`` is ``extra="ignore"``, a typo'd / unknown field
    would otherwise pass validation and leave a junk top-level key on disk. Reject it
    as ``validation`` and write nothing.
    """
    before = temp_config_path.read_text()
    base_hash = cws.compute_content_hash(temp_config_path)
    req = cws.SaveRequest(
        section="global",
        changes={"not_a_real_field": 1},
        base_hash=base_hash,
    )
    result = cws.save_section(req, temp_config_path)
    assert result.ok is False
    assert result.errors[0].code == "validation"
    assert "not_a_real_field" in result.errors[0].message
    assert temp_config_path.read_text() == before
    assert "not_a_real_field" not in yaml.safe_load(temp_config_path.read_text())


# --- T022: catalog referential integrity + delete-protection (research D6) -----


def test_create_catalog_item_persists(temp_config_path: Path):
    base_hash = cws.compute_content_hash(temp_config_path)
    result = cws.create_catalog_item(
        "endpoints",
        {"name": "extra-vllm", "kind": "vllm", "base_url": "http://h:8001"},
        base_hash,
        temp_config_path,
    )
    assert result.ok is True
    names = [e["name"] for e in yaml.safe_load(temp_config_path.read_text())["endpoints"]]
    assert "extra-vllm" in names


def test_create_catalog_item_unknown_reference_rejected(temp_config_path: Path):
    base_hash = cws.compute_content_hash(temp_config_path)
    result = cws.create_catalog_item(
        "model_endpoints",
        {"name": "bad", "endpoint": "no-such-endpoint", "model": "m"},
        base_hash,
        temp_config_path,
    )
    assert result.ok is False
    assert result.errors[0].code == "validation"
    assert "no-such-endpoint" in result.errors[0].message


def test_update_catalog_item_persists(temp_config_path: Path):
    base_hash = cws.compute_content_hash(temp_config_path)
    result = cws.update_catalog_item(
        "endpoints",
        "openai-native",
        {"auth_env": "OPENAI_KEY_2"},
        base_hash,
        temp_config_path,
    )
    assert result.ok is True
    eps = {e["name"]: e for e in yaml.safe_load(temp_config_path.read_text())["endpoints"]}
    assert eps["openai-native"]["auth_env"] == "OPENAI_KEY_2"


def test_update_catalog_item_non_mapping_changes_returns_structured_error(
    temp_config_path: Path,
):
    """A non-object ``changes`` (list/string) must NOT 500 with a TypeError on the
    ``{**it, **changes}`` merge. The service must reject it up front with a
    structured ``validation`` error (mirrors update_routing_entry's guard)."""
    base_hash = cws.compute_content_hash(temp_config_path)
    result = cws.update_catalog_item(
        "endpoints",
        "openai-native",
        ["not", "a", "mapping"],
        base_hash,
        temp_config_path,
    )
    assert result.ok is False
    assert result.errors[0].code == "validation"


def test_delete_referenced_catalog_item_blocked(temp_config_path: Path):
    base_hash = cws.compute_content_hash(temp_config_path)
    result = cws.delete_catalog_item(
        "endpoints", "local-vllm", base_hash, temp_config_path,
    )
    assert result.ok is False
    assert result.errors[0].code == "referenced"
    # The error names the referrer so the operator knows what to fix.
    assert "qwen-coder" in result.errors[0].message
    # Nothing was removed from disk.
    names = [e["name"] for e in yaml.safe_load(temp_config_path.read_text())["endpoints"]]
    assert "local-vllm" in names


def test_delete_catalog_item_fails_closed_when_refs_uncomputable(
    temp_config_path: Path, monkeypatch,
):
    """If the reference graph can't be computed (``_referrers`` raises), the delete
    must FAIL CLOSED: return a structured error and write nothing, rather than
    silently treating the item as unreferenced and deleting it (which could leave
    dangling references and violate the delete-protection guarantee)."""

    def _boom(*_args, **_kwargs):
        raise RuntimeError("reference graph blew up")

    monkeypatch.setattr(cws, "_referrers", _boom)
    base_hash = cws.compute_content_hash(temp_config_path)
    result = cws.delete_catalog_item(
        "endpoints", "openai-native", base_hash, temp_config_path,
    )
    assert result.ok is False
    assert result.errors[0].code in ("forbidden", "validation")
    # Nothing was removed from disk — the item is still present.
    names = [e["name"] for e in yaml.safe_load(temp_config_path.read_text())["endpoints"]]
    assert "openai-native" in names


def test_delete_unreferenced_catalog_item_succeeds(temp_config_path: Path):
    base_hash = cws.compute_content_hash(temp_config_path)
    result = cws.delete_catalog_item(
        "endpoints", "openai-native", base_hash, temp_config_path,
    )
    assert result.ok is True
    assert result.applied == "hot_reloaded"
    names = [e["name"] for e in yaml.safe_load(temp_config_path.read_text())["endpoints"]]
    assert "openai-native" not in names


def test_delete_catalog_item_missing_item_is_validation(temp_config_path: Path):
    """Deleting a non-existent item must fail, not silently report success.

    Mirrors ``update_catalog_item``: without an existence check the filter is a
    no-op, yet the file is rewritten and ``ok=True`` is returned — misleading the
    UI/API into reporting a successful delete that changed nothing.
    """
    before = temp_config_path.read_text()
    base_hash = cws.compute_content_hash(temp_config_path)
    result = cws.delete_catalog_item(
        "endpoints", "no-such-endpoint", base_hash, temp_config_path,
    )
    assert result.ok is False
    assert result.errors[0].code == "validation"
    assert "no-such-endpoint" in result.errors[0].message
    # Nothing written: the file is untouched.
    assert temp_config_path.read_text() == before


# --- corrupted (non-list) catalog section guard (Copilot round 29) -------------
# create/update/delete assume the on-disk catalog section is list-shaped. A
# corrupted dict-shaped section (e.g. ``endpoints: {a: ...}``) must be reported as
# structurally corrupt/read-only (`forbidden`), not iterated as keys — which would
# otherwise produce misleading "No X item named ..." / follow-on validation errors.


def _corrupt_catalog_to_dict(config_path: Path, catalog: str) -> str:
    """Rewrite ``catalog`` in the on-disk config as a (corrupt) mapping; return hash."""
    loaded = yaml.safe_load(config_path.read_text())
    loaded[catalog] = {"oops": "not-a-list"}
    config_path.write_text(yaml.safe_dump(loaded, sort_keys=False))
    return cws.compute_content_hash(config_path)


def test_create_catalog_item_corrupt_section_is_forbidden_not_misleading(
    temp_config_path: Path,
):
    base_hash = _corrupt_catalog_to_dict(temp_config_path, "endpoints")
    result = cws.create_catalog_item(
        "endpoints",
        {"name": "x", "kind": "vllm", "base_url": "http://h:8001"},
        base_hash,
        temp_config_path,
    )
    assert result.ok is False
    assert result.errors[0].code == "forbidden"


def test_update_catalog_item_corrupt_section_is_forbidden_not_misleading(
    temp_config_path: Path,
):
    base_hash = _corrupt_catalog_to_dict(temp_config_path, "endpoints")
    result = cws.update_catalog_item(
        "endpoints",
        "openai-native",
        {"auth_env": "X"},
        base_hash,
        temp_config_path,
    )
    assert result.ok is False
    assert result.errors[0].code == "forbidden"


def test_delete_catalog_item_corrupt_section_is_forbidden_not_misleading(
    temp_config_path: Path,
):
    base_hash = _corrupt_catalog_to_dict(temp_config_path, "endpoints")
    result = cws.delete_catalog_item(
        "endpoints", "openai-native", base_hash, temp_config_path,
    )
    assert result.ok is False
    assert result.errors[0].code == "forbidden"


# --- atomic_write_yaml dump-option / OSError parity (Copilot round 10) ---------
# Align formatting with the other atomic YAML writers (default_flow_style=False,
# allow_unicode=True) and convert an unwritable-file OSError into a structured
# `forbidden` SaveResult instead of letting it bubble to FastAPI as a 500.


def test_atomic_write_yaml_preserves_unicode_and_block_style(tmp_path: Path):
    p = tmp_path / "config.yaml"
    cws.atomic_write_yaml(p, {"label": "café — λ", "items": ["a", "b"]})
    text = p.read_text(encoding="utf-8")
    # allow_unicode=True: non-ASCII is written verbatim, not \uXXXX-escaped.
    assert "café — λ" in text
    assert "\\u" not in text
    # default_flow_style=False: collections render in block style, not `[a, b]`.
    assert "- a" in text
    assert "[a, b]" not in text


def test_save_section_oserror_is_forbidden_not_500(temp_config_path: Path, monkeypatch):
    base_hash = cws.compute_content_hash(temp_config_path)

    def _boom(*_a, **_k):
        raise OSError("read-only filesystem")

    monkeypatch.setattr(cws, "atomic_write_yaml", _boom)
    result = cws.save_section(
        cws.SaveRequest(
            section="global",
            changes={"poll_interval_seconds": 45},
            base_hash=base_hash,
        ),
        temp_config_path,
    )
    assert result.ok is False
    assert result.errors[0].code == "forbidden"


def test_save_section_invalid_utf8_is_forbidden_not_500(tmp_path: Path):
    """If config.yaml contains invalid UTF-8 (out-of-band corruption), the
    tolerant read path must surface a structured ``forbidden`` SaveResult rather
    than letting UnicodeDecodeError bubble to FastAPI as an unstructured 500
    (Copilot round 14)."""
    p = tmp_path / "config.yaml"
    p.write_bytes(b"poll_interval_seconds: 30\ninvalid: \xff\xfe bytes\n")
    base_hash = cws.compute_content_hash(p)
    result = cws.save_section(
        cws.SaveRequest(
            section="global",
            changes={"poll_interval_seconds": 45},
            base_hash=base_hash,
        ),
        p,
    )
    assert result.ok is False
    assert result.errors[0].code == "forbidden"


def test_safe_read_config_dict_non_file_is_forbidden_not_absent(tmp_path: Path):
    """If the config path exists but is NOT a regular file (a directory / mount
    glitch / symlink-to-dir), the write path must surface a structured
    ``forbidden`` read-only SaveResult — not silently treat it as absent (``{}``),
    which would fall through to confusing pydantic "field required" errors and
    break the docstring's "non-file yields forbidden" guarantee (Copilot round
    16)."""
    cfg = tmp_path / "config.yaml"
    cfg.mkdir()  # exists, but not a regular file
    loaded, result = cws._safe_read_config_dict(cfg, key="endpoints")
    assert loaded == {}
    assert result is not None
    assert result.ok is False
    assert result.errors[0].code == "forbidden"


def test_safe_read_config_dict_absent_is_empty_no_error(tmp_path: Path):
    """A genuinely absent config file remains the empty/absent case (``{}``, no
    error) — only *existing non-files* are forbidden (Copilot round 16)."""
    absent = tmp_path / "nope.yaml"
    loaded, result = cws._safe_read_config_dict(absent, key="endpoints")
    assert loaded == {}
    assert result is None


def test_safe_read_config_dict_empty_file_is_absent_no_error(tmp_path: Path):
    """An empty (or whitespace/comment-only) config file parses to ``None`` and
    must behave as absent (``{}``, no error) — not as a structurally corrupt
    document (Copilot round 25)."""
    cfg = tmp_path / "config.yaml"
    cfg.write_text("# just a comment, no content\n", encoding="utf-8")
    loaded, result = cws._safe_read_config_dict(cfg, key="endpoints")
    assert loaded == {}
    assert result is None


def test_safe_read_config_dict_non_mapping_root_is_forbidden(tmp_path: Path):
    """A syntactically valid YAML document whose root is NOT a mapping (a list or
    scalar) means the on-disk config is structurally corrupted. Surface it as a
    structured ``forbidden`` result rather than silently coercing to ``{}`` (which
    falls through to confusing pydantic "field required" errors and masks the real
    problem) (Copilot round 25)."""
    for body in ("- a\n- b\n", "just-a-scalar\n", "42\n"):
        cfg = tmp_path / "config.yaml"
        cfg.write_text(body, encoding="utf-8")
        loaded, result = cws._safe_read_config_dict(cfg, key="endpoints")
        assert loaded == {}, body
        assert result is not None, body
        assert result.ok is False, body
        assert result.errors[0].code == "forbidden", body


def test_create_catalog_item_oserror_is_forbidden_not_500(
    temp_config_path: Path, monkeypatch,
):
    base_hash = cws.compute_content_hash(temp_config_path)

    def _boom(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(cws, "atomic_write_yaml", _boom)
    result = cws.create_catalog_item(
        "endpoints",
        {"name": "new-ep", "kind": "openai", "auth_env": "NEW_KEY"},
        base_hash,
        temp_config_path,
    )
    assert result.ok is False
    assert result.errors[0].code == "forbidden"


def test_update_catalog_item_oserror_is_forbidden_not_500(
    temp_config_path: Path, monkeypatch,
):
    base_hash = cws.compute_content_hash(temp_config_path)

    def _boom(*_a, **_k):
        raise OSError("permission denied")

    monkeypatch.setattr(cws, "atomic_write_yaml", _boom)
    result = cws.update_catalog_item(
        "endpoints",
        "openai-native",
        {"auth_env": "OPENAI_KEY_2"},
        base_hash,
        temp_config_path,
    )
    assert result.ok is False
    assert result.errors[0].code == "forbidden"


def test_delete_catalog_item_oserror_is_forbidden_not_500(
    temp_config_path: Path, monkeypatch,
):
    base_hash = cws.compute_content_hash(temp_config_path)

    def _boom(*_a, **_k):
        raise OSError("read-only filesystem")

    monkeypatch.setattr(cws, "atomic_write_yaml", _boom)
    result = cws.delete_catalog_item(
        "endpoints", "openai-native", base_hash, temp_config_path,
    )
    assert result.ok is False
    assert result.errors[0].code == "forbidden"


# --- Tolerant read: malformed/unreadable config.yaml → structured error (round 11) ---
# save_section / catalog CRUD parse config.yaml on the read side. An out-of-band
# edit or mount glitch that leaves the file malformed/unreadable must surface as a
# structured forbidden SaveResult, never an unstructured FastAPI 500.


def test_save_section_malformed_config_is_forbidden_not_500(temp_config_path: Path):
    temp_config_path.write_text("{unbalanced: [", encoding="utf-8")
    base_hash = cws.compute_content_hash(temp_config_path)
    req = cws.SaveRequest(
        section="global",
        changes={"poll_interval_seconds": 45},
        base_hash=base_hash,
    )
    result = cws.save_section(req, temp_config_path)
    assert result.ok is False
    assert result.errors[0].code == "forbidden"


def test_create_catalog_item_malformed_config_is_forbidden_not_500(
    temp_config_path: Path,
):
    temp_config_path.write_text("{unbalanced: [", encoding="utf-8")
    base_hash = cws.compute_content_hash(temp_config_path)
    result = cws.create_catalog_item(
        "endpoints",
        {"name": "new-ep", "kind": "openai", "auth_env": "NEW_KEY"},
        base_hash,
        temp_config_path,
    )
    assert result.ok is False
    assert result.errors[0].code == "forbidden"


def test_save_section_unreadable_config_is_forbidden_not_500(
    temp_config_path: Path, monkeypatch,
):
    base_hash = cws.compute_content_hash(temp_config_path)

    def _boom(*_a, **_k):
        raise OSError("permission denied")

    monkeypatch.setattr(Path, "read_text", _boom)
    req = cws.SaveRequest(
        section="global",
        changes={"poll_interval_seconds": 45},
        base_hash=base_hash,
    )
    result = cws.save_section(req, temp_config_path)
    assert result.ok is False
    assert result.errors[0].code == "forbidden"


def test_save_section_concurrency_guard_oserror_is_forbidden_not_500(
    temp_config_path: Path, monkeypatch,
):
    # guard_concurrency() can raise OSError (unreadable path / directory due to a
    # mount glitch). _conflict_result must convert it to a structured forbidden
    # SaveResult, not let it bubble as a 500.
    base_hash = cws.compute_content_hash(temp_config_path)

    def _boom(*_a, **_k):
        raise OSError("config path is unreadable")

    monkeypatch.setattr(cws, "guard_concurrency", _boom)
    req = cws.SaveRequest(
        section="global",
        changes={"poll_interval_seconds": 45},
        base_hash=base_hash,
    )
    result = cws.save_section(req, temp_config_path)
    assert result.ok is False
    assert result.errors[0].code == "forbidden"


# --- Defensive catalog guard: reject unknown catalog keys (Copilot round 7) ----
# ProjectConfiguration is extra="ignore", so a mis-routed internal call with an
# unsupported catalog name would otherwise persist a junk top-level key into
# config.yaml without a validation error. Mirror the dashboard endpoint guard.


def test_create_catalog_item_unknown_catalog_rejected(temp_config_path: Path):
    before = temp_config_path.read_text()
    base_hash = cws.compute_content_hash(temp_config_path)
    result = cws.create_catalog_item(
        "not_a_catalog",
        {"name": "junk"},
        base_hash,
        temp_config_path,
    )
    assert result.ok is False
    assert result.errors[0].code == "validation"
    assert "not_a_catalog" in result.errors[0].message
    # Nothing written: no junk top-level key persisted.
    assert temp_config_path.read_text() == before
    assert "not_a_catalog" not in yaml.safe_load(temp_config_path.read_text())


def test_update_catalog_item_unknown_catalog_rejected(temp_config_path: Path):
    before = temp_config_path.read_text()
    base_hash = cws.compute_content_hash(temp_config_path)
    result = cws.update_catalog_item(
        "not_a_catalog",
        "whatever",
        {"x": 1},
        base_hash,
        temp_config_path,
    )
    assert result.ok is False
    assert result.errors[0].code == "validation"
    assert "not_a_catalog" in result.errors[0].message
    assert temp_config_path.read_text() == before


def test_delete_catalog_item_unknown_catalog_rejected(temp_config_path: Path):
    before = temp_config_path.read_text()
    base_hash = cws.compute_content_hash(temp_config_path)
    result = cws.delete_catalog_item(
        "not_a_catalog", "whatever", base_hash, temp_config_path,
    )
    assert result.ok is False
    assert result.errors[0].code == "validation"
    assert "not_a_catalog" in result.errors[0].message
    assert temp_config_path.read_text() == before
