"""Unit tests for the spec-081 routing-config service (T035).

Exercises the routing-table read / validate / write path against the spec-078
models (``RoutingTable``/``RoutingEntry``/``TargetDescriptor`` reused from
``performer.proxy.routing``):

  * ``locate_routing_file`` / ``routing_available`` host-path resolution.
  * ``read_routing`` — entries + content_hash when present; empty read-only state
    when absent / unmounted.
  * ``validate_routing_entry`` — reroute ⇒ empty normalizers; normalize ⇒
    non-empty, all in ``NORMALIZER_REGISTRY``; wire_format ∈ {openai, anthropic};
    non-empty base_url.
  * create / update / delete — concurrency-guarded, validated, atomically written,
    all ``applied: staged_next_job``.

The coordinare and performer suites run separately (conftest collision). These
tests reuse the coordinare-suite ``temp_config_with_routing`` /
``temp_routing_path`` fixtures and ``performer`` is importable from the coordinare
venv for validation parity.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from coordinare import routing_config_service as rcs
from coordinare.config import CoordinareConfiguration
from coordinare.config_validation import coerce_multi_symphony_raw
from coordinare.services.config_write_service import compute_content_hash


def _endpoints_from_config(config_path: Path):
    raw = yaml.safe_load(config_path.read_text())
    cfg = CoordinareConfiguration(
        **coerce_multi_symphony_raw({**raw, "github_token": "ghp_fixturetoken"})
    )
    return cfg.global_config.performer_endpoints


# --- location resolution ------------------------------------------------------


def test_locate_routing_file_resolves_host_path(temp_config_with_routing):
    config_path, routing_path = temp_config_with_routing
    endpoints = _endpoints_from_config(config_path)
    location = rcs.locate_routing_file(endpoints)
    assert location is not None
    assert location.host_path == routing_path
    assert location.container_path == "/devenv/routing.yaml"


def test_routing_available_true_when_file_exists(temp_config_with_routing):
    config_path, _routing_path = temp_config_with_routing
    endpoints = _endpoints_from_config(config_path)
    assert rcs.routing_available(endpoints) is True


def test_routing_available_false_when_host_path_missing(temp_config_path):
    # REPRESENTATIVE_CONFIG mounts /host/routing.yaml which does not exist.
    endpoints = _endpoints_from_config(temp_config_path)
    assert rcs.routing_available(endpoints) is False


def test_routing_available_false_when_present_file_unreadable(
    temp_config_with_routing, monkeypatch
):
    """``routing_available()`` must agree with ``read_routing()`` on a present-
    but-unreadable mounted file: both report unavailable. Otherwise
    ``/api/config/all`` reports ``routing_available=True`` (it only checks
    ``is_file()``) while the routing endpoint reports the read-only empty state,
    so the UI presents routing as editable until the routing fetch corrects it
    (Copilot round 27). The readability re-probe is shared so the two cannot
    drift apart."""
    config_path, routing_path = temp_config_with_routing
    endpoints = _endpoints_from_config(config_path)
    # Sanity: the file exists, so the old is_file()-only check returned True.
    assert routing_path.is_file()

    real_read_bytes = Path.read_bytes

    def _unreadable(self, *a, **k):
        if self == routing_path:
            raise OSError("permission denied")
        return real_read_bytes(self, *a, **k)

    monkeypatch.setattr(Path, "read_bytes", _unreadable)
    assert rcs.routing_available(endpoints) is False


# --- read_routing -------------------------------------------------------------


def test_read_routing_returns_entries_and_hash(temp_config_with_routing):
    config_path, routing_path = temp_config_with_routing
    endpoints = _endpoints_from_config(config_path)
    location = rcs.locate_routing_file(endpoints)
    view = rcs.read_routing(location)
    assert view.routing_available is True
    assert view.content_hash == compute_content_hash(routing_path)
    assert len(view.entries) == 1
    assert view.entries[0]["backend"] == "vllm"
    assert view.entries[0]["model"] == "qwen2.5-coder"


def test_read_routing_none_location_is_empty_state():
    view = rcs.read_routing(None)
    assert view.routing_available is False
    assert view.entries == []
    assert view.content_hash is None


def test_read_routing_missing_host_path_is_empty_state(tmp_path):
    location = rcs.RoutingLocation(
        endpoint_id="x",
        host_path=tmp_path / "nope.yaml",
        container_path="/devenv/routing.yaml",
    )
    view = rcs.read_routing(location)
    assert view.routing_available is False
    assert view.entries == []


# --- validate_routing_entry ---------------------------------------------------


def _entry(strategy="normalize", normalizers=("harmony_tool_calls",), **target):
    t = {
        "base_url": "http://localhost:8000/v1",
        "wire_format": "openai",
        "strategy": strategy,
        "normalizers": list(normalizers),
        **target,
    }
    return {"backend": "vllm", "model": "qwen2.5-coder", "target": t}


def test_validate_routing_entry_accepts_valid_normalize():
    rcs.validate_routing_entry(_entry())


def test_validate_routing_entry_accepts_valid_reroute():
    rcs.validate_routing_entry(
        _entry(strategy="reroute", normalizers=[], reroute_upstream="http://up")
    )


def test_validate_routing_entry_reroute_with_normalizers_rejected():
    with pytest.raises(ValueError):
        rcs.validate_routing_entry(
            _entry(strategy="reroute", normalizers=["harmony_tool_calls"])
        )


def test_validate_routing_entry_normalize_without_normalizers_rejected():
    with pytest.raises(ValueError):
        rcs.validate_routing_entry(_entry(strategy="normalize", normalizers=[]))


def test_validate_routing_entry_unknown_normalizer_rejected():
    with pytest.raises(ValueError):
        rcs.validate_routing_entry(_entry(normalizers=["no_such_normalizer"]))


def test_validate_routing_entry_bad_wire_format_rejected():
    with pytest.raises(ValueError):
        rcs.validate_routing_entry(_entry(wire_format="grpc"))


def test_validate_routing_entry_empty_base_url_rejected():
    with pytest.raises(ValueError):
        rcs.validate_routing_entry(_entry(base_url=""))


def _block_performer_routing_import(monkeypatch):
    """Make ``import performer.proxy.routing`` raise ModuleNotFoundError, as it
    would in the coordinare daemon image (Dockerfile.daemon copies only ``src`` and
    runs ``uv sync``; the performer package is not a coordinare dependency)."""
    import builtins

    real_import = builtins.__import__

    def _fake_import(name, *args, **kwargs):
        if name == "performer.proxy.routing":
            raise ModuleNotFoundError("No module named 'performer'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _fake_import)


def test_validate_routing_entry_missing_performer_module_raises_valueerror(monkeypatch):
    """When the performer routing models are not installed (production daemon
    image), ``validate_routing_entry`` must convert the ModuleNotFoundError into a
    ValueError so callers return a structured ``validation`` result instead of a
    500 (Copilot round 28)."""
    _block_performer_routing_import(monkeypatch)
    with pytest.raises(ValueError):
        rcs.validate_routing_entry(_entry())


def test_validate_table_missing_performer_module_raises_valueerror(monkeypatch):
    """``_validate_table`` mirrors ``validate_routing_entry``: a missing performer
    package becomes a ValueError, not a 500 (Copilot round 28)."""
    _block_performer_routing_import(monkeypatch)
    with pytest.raises(ValueError):
        rcs._validate_table([_entry()])


def test_create_routing_entry_missing_performer_module_is_structured_not_500(
    tmp_path, monkeypatch
):
    """End-to-end: with the performer package absent, a routing write returns a
    structured ``validation`` ``SaveResult`` (no crash) rather than 500ing the
    routing endpoint (Copilot round 28)."""
    f = tmp_path / "routing.yaml"
    f.write_text("entries: []\n", encoding="utf-8")
    location = rcs.RoutingLocation(
        endpoint_id="x", host_path=f, container_path="/devenv/routing.yaml"
    )
    _block_performer_routing_import(monkeypatch)
    result = rcs.create_routing_entry(location, _entry(), compute_content_hash(f))
    assert result.ok is False
    assert result.errors[0].code == "validation"


# --- create / update / delete -------------------------------------------------


def _base_hash(routing_path: Path) -> str:
    return compute_content_hash(routing_path)


def test_create_routing_entry_appends_and_stages_next_job(temp_config_with_routing):
    config_path, routing_path = temp_config_with_routing
    location = rcs.locate_routing_file(_endpoints_from_config(config_path))
    new = _entry()
    new["model"] = "llama-3"
    result = rcs.create_routing_entry(location, new, _base_hash(routing_path))
    assert result.ok is True
    assert result.applied == "staged_next_job"
    assert result.new_hash.startswith("sha256:")
    on_disk = yaml.safe_load(routing_path.read_text())["entries"]
    assert {e["model"] for e in on_disk} == {"qwen2.5-coder", "llama-3"}


def test_create_routing_entry_oserror_is_forbidden_not_500(
    temp_config_with_routing, monkeypatch
):
    config_path, routing_path = temp_config_with_routing
    location = rcs.locate_routing_file(_endpoints_from_config(config_path))
    new = _entry()
    new["model"] = "llama-3"

    from coordinare.services import config_write_service as cws

    def _boom(*_a, **_k):
        raise OSError("read-only filesystem")

    monkeypatch.setattr(cws, "atomic_write_yaml", _boom)
    result = rcs.create_routing_entry(location, new, _base_hash(routing_path))
    assert result.ok is False
    assert result.errors[0].code == "forbidden"


def test_create_routing_entry_invalid_rejected(temp_config_with_routing):
    config_path, routing_path = temp_config_with_routing
    location = rcs.locate_routing_file(_endpoints_from_config(config_path))
    bad = _entry(strategy="reroute", normalizers=["harmony_tool_calls"])
    result = rcs.create_routing_entry(location, bad, _base_hash(routing_path))
    assert result.ok is False
    assert result.errors[0].code == "validation"
    # Nothing was written.
    assert len(yaml.safe_load(routing_path.read_text())["entries"]) == 1


def test_create_routing_entry_conflict(temp_config_with_routing):
    config_path, _routing_path = temp_config_with_routing
    location = rcs.locate_routing_file(_endpoints_from_config(config_path))
    result = rcs.create_routing_entry(location, _entry(), "sha256:stale")
    assert result.ok is False
    assert result.errors[0].code == "conflict"


def test_create_routing_entry_rechecks_conflict_just_before_write(
    temp_config_with_routing, monkeypatch
):
    """FR-016: an out-of-band edit landing after the initial conflict check but
    before the atomic swap must be caught (re-check immediately before write)."""
    config_path, routing_path = temp_config_with_routing
    location = rcs.locate_routing_file(_endpoints_from_config(config_path))
    base_hash = _base_hash(routing_path)
    real_validate = rcs._validate_table

    def mutate_then_validate(entries):
        # Simulate a concurrent operator edit landing after the initial baseline.
        routing_path.write_text(
            routing_path.read_text() + "\n# concurrent out-of-band edit\n"
        )
        return real_validate(entries)

    monkeypatch.setattr(rcs, "_validate_table", mutate_then_validate)
    result = rcs.create_routing_entry(location, _entry(), base_hash)
    assert result.ok is False
    assert result.errors[0].code == "conflict"


def test_update_routing_entry_applies_changes(temp_config_with_routing):
    config_path, routing_path = temp_config_with_routing
    location = rcs.locate_routing_file(_endpoints_from_config(config_path))
    result = rcs.update_routing_entry(
        location, 0, {"model": "qwen2.5-coder-32b"}, _base_hash(routing_path)
    )
    assert result.ok is True
    assert result.applied == "staged_next_job"
    on_disk = yaml.safe_load(routing_path.read_text())["entries"]
    assert on_disk[0]["model"] == "qwen2.5-coder-32b"


def test_update_routing_entry_non_mapping_target_returns_structured_error(
    temp_config_with_routing,
):
    """A corrupted/malformed routing.yaml whose entry ``target`` is a non-mapping
    (string/list) must NOT 500 with a TypeError on ``**`` unpacking. The merge must
    tolerate it so ``_validate_table`` can return a structured validation error."""
    config_path, routing_path = temp_config_with_routing
    doc = yaml.safe_load(routing_path.read_text())
    doc["entries"][0]["target"] = "not-a-mapping"  # corrupt the on-disk target
    routing_path.write_text(yaml.safe_dump(doc))
    location = rcs.locate_routing_file(_endpoints_from_config(config_path))
    # Apply a dict target change → previously hit `{**"not-a-mapping"}` → TypeError.
    result = rcs.update_routing_entry(
        location, 0, {"target": {"base_url": "http://x/v1"}}, _base_hash(routing_path)
    )
    assert result.ok is False
    assert result.errors[0].code == "validation"


def test_update_routing_entry_non_mapping_changes_returns_structured_error(
    temp_config_with_routing,
):
    """A client sending a non-object ``changes`` (list/string) must NOT 500 with a
    TypeError on ``dict(changes)``. The service must reject it up front with a
    structured ``validation`` error so the API returns a 422-style result."""
    config_path, routing_path = temp_config_with_routing
    location = rcs.locate_routing_file(_endpoints_from_config(config_path))
    result = rcs.update_routing_entry(
        location, 0, ["not", "a", "mapping"], _base_hash(routing_path)
    )
    assert result.ok is False
    assert result.errors[0].code == "validation"


def test_update_routing_entry_out_of_range(temp_config_with_routing):
    config_path, routing_path = temp_config_with_routing
    location = rcs.locate_routing_file(_endpoints_from_config(config_path))
    result = rcs.update_routing_entry(
        location, 9, {"model": "x"}, _base_hash(routing_path)
    )
    assert result.ok is False
    assert result.errors[0].code == "validation"


def test_update_routing_entry_invalid_change_rejected(temp_config_with_routing):
    config_path, routing_path = temp_config_with_routing
    location = rcs.locate_routing_file(_endpoints_from_config(config_path))
    # Empty the model field → invalid (min_length=1).
    result = rcs.update_routing_entry(
        location, 0, {"model": ""}, _base_hash(routing_path)
    )
    assert result.ok is False
    assert result.errors[0].code == "validation"


def test_delete_routing_entry_removes_row(temp_config_with_routing):
    config_path, routing_path = temp_config_with_routing
    location = rcs.locate_routing_file(_endpoints_from_config(config_path))
    result = rcs.delete_routing_entry(location, 0, _base_hash(routing_path))
    assert result.ok is True
    assert result.applied == "staged_next_job"
    assert yaml.safe_load(routing_path.read_text())["entries"] == []


def test_delete_routing_entry_out_of_range(temp_config_with_routing):
    config_path, routing_path = temp_config_with_routing
    location = rcs.locate_routing_file(_endpoints_from_config(config_path))
    result = rcs.delete_routing_entry(location, 5, _base_hash(routing_path))
    assert result.ok is False
    assert result.errors[0].code == "validation"


# --- read tolerance + base_hash validation parity -----------------------------


def test_read_routing_tolerates_malformed_yaml(tmp_path):
    """A syntactically invalid routing YAML must not 500 the display fetch; it
    degrades to the read-only empty state (the read is documented as tolerant)."""
    bad = tmp_path / "routing.yaml"
    bad.write_text("entries: [unterminated\n  - : :\n", encoding="utf-8")
    location = rcs.RoutingLocation(
        endpoint_id="x", host_path=bad, container_path="/devenv/routing.yaml"
    )
    view = rcs.read_routing(location)
    # File exists, so routing is available, but the unparseable body yields no entries.
    assert view.routing_available is True
    assert view.entries == []


def test_read_routing_tolerates_invalid_utf8(tmp_path):
    """A routing YAML with invalid UTF-8 bytes must not 500 the display fetch;
    UnicodeDecodeError is treated like YAMLError/OSError and degrades to the
    empty entry list (Copilot round 14)."""
    bad = tmp_path / "routing.yaml"
    bad.write_bytes(b"entries:\n  - model: \xff\xfe\n")
    location = rcs.RoutingLocation(
        endpoint_id="x", host_path=bad, container_path="/devenv/routing.yaml"
    )
    view = rcs.read_routing(location)
    assert view.routing_available is True
    assert view.entries == []


def test_read_routing_unreadable_present_file_is_unavailable(tmp_path, monkeypatch):
    """A present-but-unreadable mounted routing file must report the read-only
    empty state (``routing_available=False``, ``content_hash=None``), NOT
    ``routing_available=True`` with a spurious empty-file sentinel hash.

    ``compute_content_hash`` is total — it swallows the read OSError and returns
    the empty-file sentinel — so the old ``except OSError`` around it was dead code
    (Copilot round 25). The hash alone cannot distinguish a genuinely empty file
    from an unreadable one, so ``read_routing`` must re-probe readability when the
    hash equals the sentinel."""
    f = tmp_path / "routing.yaml"
    f.write_text("entries: []\n", encoding="utf-8")
    location = rcs.RoutingLocation(
        endpoint_id="x", host_path=f, container_path="/devenv/routing.yaml"
    )

    real_read_bytes = Path.read_bytes

    def _unreadable(self, *a, **k):
        if self == f:
            raise OSError("permission denied")
        return real_read_bytes(self, *a, **k)

    monkeypatch.setattr(Path, "read_bytes", _unreadable)
    view = rcs.read_routing(location)
    assert view.routing_available is False
    assert view.entries == []
    assert view.content_hash is None


def test_read_routing_empty_file_remains_available(tmp_path):
    """A genuinely empty routing file (zero bytes) reads back cleanly: it is
    readable, so it stays ``routing_available=True`` with the empty-file sentinel
    hash and no entries — the readability re-probe must NOT misclassify it as
    unavailable (Copilot round 25)."""
    f = tmp_path / "routing.yaml"
    f.write_text("", encoding="utf-8")
    location = rcs.RoutingLocation(
        endpoint_id="x", host_path=f, container_path="/devenv/routing.yaml"
    )
    view = rcs.read_routing(location)
    assert view.routing_available is True
    assert view.entries == []
    assert view.content_hash is not None


def test_routing_available_false_when_host_path_is_directory(temp_config_path, tmp_path):
    """A host_path that resolves to a directory must not present routing as
    editable: reads degrade to empty and writes would OSError. Treat a non-file
    the same as a missing file."""
    a_dir = tmp_path / "routing.yaml"
    a_dir.mkdir()
    location = rcs.RoutingLocation(
        endpoint_id="x", host_path=a_dir, container_path="/devenv/routing.yaml"
    )
    # read_routing must report the read-only empty state, not "available".
    view = rcs.read_routing(location)
    assert view.routing_available is False
    assert view.entries == []
    assert view.content_hash is None


def test_create_routing_entry_directory_host_path_is_readonly(tmp_path):
    """A host_path that is a directory (or otherwise not a file) must be treated
    as read-only — never created/overwritten — matching routing_available()."""
    a_dir = tmp_path / "routing.yaml"
    a_dir.mkdir()
    location = rcs.RoutingLocation(
        endpoint_id="x", host_path=a_dir, container_path="/devenv/routing.yaml"
    )
    result = rcs.create_routing_entry(location, _entry(), "sha256:whatever")
    assert result.ok is False
    assert result.errors[0].code == "forbidden"
    # Nothing was written: the directory is untouched (no file created inside).
    assert list(a_dir.iterdir()) == []


def test_update_routing_entry_missing_host_path_is_readonly(tmp_path):
    location = rcs.RoutingLocation(
        endpoint_id="x",
        host_path=tmp_path / "nope.yaml",
        container_path="/devenv/routing.yaml",
    )
    result = rcs.update_routing_entry(location, 0, {"model": "x"}, "sha256:whatever")
    assert result.ok is False
    assert result.errors[0].code == "forbidden"
    assert not (tmp_path / "nope.yaml").exists()


def test_delete_routing_entry_directory_host_path_is_readonly(tmp_path):
    a_dir = tmp_path / "routing.yaml"
    a_dir.mkdir()
    location = rcs.RoutingLocation(
        endpoint_id="x", host_path=a_dir, container_path="/devenv/routing.yaml"
    )
    result = rcs.delete_routing_entry(location, 0, "sha256:whatever")
    assert result.ok is False
    assert result.errors[0].code == "forbidden"
    assert list(a_dir.iterdir()) == []


def test_readonly_guard_message_covers_non_file_host_path(tmp_path):
    """The forbidden message must not always claim 'no endpoint mounts routing':
    when an endpoint *does* mount a path that is missing / not a file, the message
    should make that distinguishable so operators can troubleshoot the mount."""
    a_dir = tmp_path / "routing.yaml"
    a_dir.mkdir()
    location = rcs.RoutingLocation(
        endpoint_id="x", host_path=a_dir, container_path="/devenv/routing.yaml"
    )
    mounted = rcs._readonly_guard(location)
    unmounted = rcs._readonly_guard(None)
    # Both are forbidden, but the messages must differ so the mounted-but-missing
    # case is not misreported as "no endpoint mounts a routing table".
    assert mounted.errors[0].code == "forbidden"
    assert unmounted.errors[0].code == "forbidden"
    assert mounted.errors[0].message != unmounted.errors[0].message


def test_routing_conflict_oserror_is_forbidden_not_500(tmp_path, monkeypatch):
    """If host_path is a real file but compute_content_hash raises OSError
    (permissions / mount glitch), the write path must degrade to a forbidden
    read-only SaveResult rather than letting the OSError 500 the endpoint."""
    host_path = tmp_path / "routing.yaml"
    host_path.write_text("entries: []\n", encoding="utf-8")

    from coordinare.services import config_write_service as cws

    def _boom(*_a, **_k):
        raise OSError("simulated unreadable mount")

    monkeypatch.setattr(cws, "guard_concurrency", _boom)
    result = rcs._routing_conflict(host_path, "sha256:whatever")
    assert result is not None
    assert result.ok is False
    assert result.errors[0].code == "forbidden"


def test_create_routing_entry_empty_base_hash_is_validation(temp_config_with_routing):
    """An empty base_hash is a client error → 'validation', not a misleading
    'conflict' (parity with config_write_service._conflict_result)."""
    config_path, _routing_path = temp_config_with_routing
    location = rcs.locate_routing_file(_endpoints_from_config(config_path))
    result = rcs.create_routing_entry(location, _entry(), "")
    assert result.ok is False
    assert result.errors[0].code == "validation"
    assert "base_hash" in result.errors[0].message
