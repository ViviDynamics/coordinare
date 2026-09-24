"""Version guards and assistant gating for the dashboard (436)."""
from __future__ import annotations

from typing import Any

import structlog
from fastapi.responses import JSONResponse

_log = structlog.get_logger(__name__)


def _entity_tags(supplied: Any) -> list[str]:
    """The versions an ``If-Match`` header names, as bare hashes.

    158 (#241). RFC 7232 3.2:
    ``If-Match = "*" / [ entity-tag *( OWS "," OWS entity-tag ) ]``, and an
    entity-tag may be weak (``W/"..."``). The dashboard's own JS sends exactly one
    strong tag, so the parse was written for that -- and every other legal shape
    then failed as a *version conflict*, which is a lie about what went wrong.

    A seventh review round: splitting on every comma and then removing quotes is the
    obvious order and the wrong one. ``etagc`` is ``%x21 / %x23-7E``, so a comma is
    legal *inside* a tag, and ``"abc,def"`` was being torn into two. The scan below
    only separates at a comma outside quotes. No version this deployment issues
    contains one, and the failure was to refuse rather than to permit, but a parser
    that answers the wrong question about a legal input is a defect regardless of
    who is currently asking.

    The wildcard is returned as ``"*"`` for the caller to refuse; it is the one tag
    that cannot be compared against a hash, and honouring it would mean writing
    without holding a version.
    """
    raw = str(supplied).strip()
    if raw == "*":
        return ["*"]

    members: list[str] = []
    buffer: list[str] = []
    quoted = False
    for char in raw:
        if char == '"':
            quoted = not quoted
        elif char == "," and not quoted:
            members.append("".join(buffer))
            buffer = []
            continue
        buffer.append(char)
    members.append("".join(buffer))

    tags: list[str] = []
    for member in members:
        tag = member.strip()
        # A weak validator names the same version as the bare hash a caller may be
        # holding: these tags are content hashes, so weak and strong comparison
        # coincide. RFC 7232 says weak tags SHOULD NOT be sent on If-Match; refusing
        # one would fail a write over a prefix rather than over the version. The
        # strip afterwards is for `W/ "x"`, which is malformed -- there is no space
        # in the grammar -- and was leaving a stray quote welded to the hash.
        if tag.startswith("W/"):
            tag = tag[2:].strip()
        # A matched pair only, so a caller holding the bare hash (which is what the
        # body vehicle hands out, and what the 081 writes take) is left alone.
        if len(tag) >= 2 and tag.startswith('"') and tag.endswith('"'):
            tag = tag[1:-1]
        tag = tag.strip()
        if tag:
            tags.append(tag)
    return tags

def _missing_version_refusal(field: str) -> JSONResponse:
    """The 428 body for a write that carries no version header at all."""
    return JSONResponse(
        {
            "error": (
                f"{field} is required. The matching GET returns the current version "
                "as an ETag; send it back so a concurrent edit is refused rather "
                "than overwritten."
            ),
            "precondition_required": True,
        },
        status_code=428,
    )


def _no_entity_tag_refusal(field: str) -> JSONResponse:
    """The 428 body for a header that parses to no entity-tag at all."""
    return JSONResponse(
        {
            "error": (
                f"{field} names no version. The matching GET returns the current "
                "version as an ETag; send it back so a concurrent edit is refused "
                "rather than overwritten."
            ),
            "precondition_required": True,
        },
        status_code=428,
    )


def _wildcard_refusal(field: str) -> JSONResponse:
    """The 428 body for an ``If-Match: *`` wildcard."""
    return JSONResponse(
        {
            "error": (
                f"{field}: * is not accepted here. A write needs the version it "
                "is replacing, so a concurrent edit can be refused; the matching "
                "GET returns it as an ETag."
            ),
            "precondition_required": True,
        },
        status_code=428,
    )


def _unreadable_refusal(exc: OSError) -> JSONResponse:
    # guard_concurrency deliberately lets OSError through -- its docstring says
    # callers should "degrade to a structured forbidden result rather than
    # proceeding", because compute_content_hash returns the empty-file sentinel
    # for a present-but-unreadable file and a bare comparison would let that
    # sentinel match and clobber data nobody read. Catching only the conflict
    # turned that into a 500 with a traceback. 403 and this message are what
    # _conflict_result already returns for the 081 writes.
    # str(OSError) carries the absolute path and this body goes to the browser;
    # safe_failure_reason keeps the useful half. The full exception goes to the log.
    from coordinare.services.config_write_service import safe_failure_reason

    _log.warning("config.write_refused_unreadable", error=str(exc))
    return JSONResponse(
        {"error": f"Could not read the configuration file: {safe_failure_reason(exc)}"},
        status_code=403,
    )


def _version_refusal(
    config_path: Any, supplied: str | None, *, field: str = "If-Match",
) -> JSONResponse | None:
    """Refuse a write that carries no usable version, or ``None`` to proceed.

    158 (#241): one implementation, because five routes needed this and five copies
    of a concurrency check is five chances to get it subtly different -- and the
    difference would show up as a lost edit, which is the failure nobody notices.

    Callers that already have their own body-field version (the global config write,
    the catalog writes) keep it; this is what the rest use.
    """
    from coordinare.services.config_write_service import (
        ConcurrencyConflictError,
        guard_concurrency,
    )

    # No file, nothing to overwrite. The writers already no-op in this case, so
    # demanding a version here would refuse a write that was never going to happen
    # -- and there would be no version to give, since the version *is* the file.
    if config_path is None or not config_path.is_file():
        return None

    if supplied is None or not str(supplied).strip():
        _log.warning("config.write_refused_unguarded", field=field)
        return _missing_version_refusal(field)

    # RFC 7232 3.2 lets If-Match carry a comma-separated list of entity-tags, with
    # optional whitespace, each optionally weak. The parse used to be
    # `removeprefix("W/").strip('"')`, which is right for the one shape this
    # dashboard's own JS sends and wrong for every other legal one -- and it failed
    # them as a 409 saying the config had changed, which is untrue and sends an
    # operator looking for a concurrent edit that never happened.
    tags = _entity_tags(supplied)

    # A header that parses to no tag at all (`,,`, `""`) names no version, and an
    # empty loop below would fall through as though the guard had passed. It is the
    # same situation as a missing header, so it gets the same answer.
    if not tags:
        _log.warning("config.write_refused_unguarded", field=field, reason="no_entity_tag")
        return _no_entity_tag_refusal(field)

    # `*` means "any current representation", i.e. write without holding a version.
    # That is precisely what this spec removed, so it is refused rather than honoured
    # -- but refused as a wildcard, so the message says what is actually wrong.
    if "*" in tags:
        _log.warning("config.write_refused_wildcard", field=field)
        return _wildcard_refusal(field)

    # Any-match, per RFC 7232: the caller holds a version that is current. Sorting the
    # conflict to last keeps the error the one a stale caller should see.
    # Flat rather than nested: `test_both_guards_catch_it` reads the try that holds
    # each guard_concurrency call, and an inner try catching only the conflict fails
    # it even when an outer one catches OSError. Over-strict in the safe direction,
    # and this reads better anyway.
    conflict: ConcurrencyConflictError | None = None
    for tag in tags:
        try:
            guard_concurrency(config_path, tag)
        except ConcurrencyConflictError as err:
            conflict = err
            continue
        except OSError as exc:
            return _unreadable_refusal(exc)
        conflict = None
        break
    if conflict is not None:
        return JSONResponse({"error": str(conflict), "conflict": True}, status_code=409)
    return None

def _version_headers(config_path: Any) -> dict[str, str]:
    """The current version of the config file, as an ``ETag`` header.

    158 (#241): the counterpart to :func:`_version_refusal`, and the reason that
    function can insist on a version at all. A guard that demands a version the
    client has no way to obtain is not a guard, it is an outage -- and this is the
    shape 156 already chose for the global config page: the header, so a response
    whose body is a values contract stays one.

    Emitted by the GETs a page loads from *and* by the writes themselves, carrying
    the post-write version, so a second save in the same session does not have to
    re-read the page to find out what it just created.

    The front end keeps one of these per editing page (``_symCfgHash``,
    ``_personaCfgHash``, and 156's ``_adminCfgHash``). One config file and three
    variables looks like redundancy and must not be consolidated: the version has to
    be bound to the *data the page is showing*, not to the file's latest state.
    Consolidated, you load Personas (hash H1, text T1), someone else edits that
    persona (file now H2), you visit Symphonies whose GET refreshes the shared hash to
    H2, you return and save T1 -- the guard sees a matching hash and overwrites their
    change without a word. Separate variables make that a 409, which is the entire
    point of the exercise.

    This docstring is also where that argument lives rather than in a comment beside
    the JS, because everything inside the dashboard HTML ships to every browser and
    counts against the 160 KB budget, which currently has under 1% of headroom.
    """
    if config_path is None or not config_path.is_file():
        return {}
    from coordinare.services.config_write_service import compute_content_hash

    # Quoted, per RFC 7232 §2.3. _version_refusal strips the quotes back off.
    return {"ETag": f'"{compute_content_hash(config_path)}"'}

def _config_assistant_enabled(daemon: Any) -> bool:
    """Is the config assistant switched on for this deployment?

    Reads the live config rather than a captured value so a reload does not leave
    the flag stale. Defaults to off on anything unexpected: a feature that talks to
    a model endpoint and reads configuration should require a deliberate yes, not
    survive an ambiguous one.
    """
    try:
        cfg = daemon.state.get("coordinare_config")
    except Exception:
        return False
    if cfg is None:
        return False
    # CoordinareConfiguration is the multi-symphony root; the global settings live on
    # its `global_config`. Accept either shape so a caller holding a
    # ProjectConfiguration directly is not silently treated as "disabled".
    holder = getattr(cfg, "global_config", cfg)
    return bool(getattr(holder, "config_assistant_enabled", False))
