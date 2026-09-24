"""Spec 158 / issue #241 — every config write takes a version.

Optimistic concurrency arrived piecemeal: spec 081 guarded the catalog and routing
writes, 156 and 157 the global one. Five routes writing the same file still took no
version, which is worse than none of them doing so — an operator who meets the
strict endpoint reasonably concludes the surface is strict.

The load-bearing test here is the enumeration. Listing today's five would pass
forever while a sixth was added tomorrow; walking the route handlers means the next
unguarded writer fails this file rather than being noticed by someone.
"""

from __future__ import annotations

import ast
import re
from functools import lru_cache
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml
from fastapi.testclient import TestClient

from coordinare.config import CoordinareConfiguration
from coordinare.config_validation import coerce_multi_symphony_raw
from coordinare.dashboard import _DASHBOARD_JS_SOURCES, DashboardStore, create_dashboard_app
from coordinare.services.config_write_service import compute_content_hash

DASHBOARD = Path("src/coordinare/dashboard")


def _frontend_source() -> str:
    """Everything the browser executes: the inline Python string plus the JS the
    349 extraction moved to /static files, concatenated for scanning.

    436: also the templates, because that is where the inline JS lives now.
    """
    parts = [_dashboard_source()]
    parts.append("\n".join(_DASHBOARD_JS_SOURCES.values()))
    parts.extend(
        path.read_text() for path in sorted((DASHBOARD / "templates").glob("*"))
    )
    return "\n".join(parts)

#: Anything that ends up writing config.yaml. A route reaching one of these without
#: a version check is the defect this spec exists to remove.
WRITERS = frozenset(
    {
        "_persist_symphony_configs",
        "reset_persona",
        "save_persona",
        "update_persona",
        "save_section",
        "atomic_write_yaml",
        # The 081 catalog and routing writes reach the file through their own service
        # functions. They are guarded, by a body `base_hash` rather than a header --
        # but naming them here is what makes the enumeration below cover the whole
        # write surface instead of the part this spec happened to touch. Without them
        # a new unguarded sibling of these routes would pass unnoticed.
        "create_catalog_item",
        "update_catalog_item",
        "delete_catalog_item",
        "create_routing_entry",
        "update_routing_entry",
        "delete_routing_entry",
    },
)


def _reaching_a_writer() -> frozenset[str]:
    return _reaching_a_writer_for(_dashboard_source())


def _compute_reaching_a_writer(source: str) -> frozenset[str]:
    """:data:`WRITERS`, plus every function in the module that reaches one.

    A sixth review round: the scan matched writer names as text in the handler's own
    body, so a handler delegating to a local helper that writes -- `return
    _do_the_save(request)` -- reached the file while looking like it touched nothing,
    and the enumeration would not have asked it for a version. Closing over the call
    graph costs a fixpoint and removes the whole class.
    """
    bodies = {
        node.name: ast.unparse(node)
        for node in ast.walk(ast.parse(source))
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    reaching = set(WRITERS)
    changed = True
    while changed:
        changed = False
        for name, body in bodies.items():
            if name in reaching:
                continue
            # `w != name` so a recursive call cannot make a function reach itself.
            if any(f"{w}(" in body for w in reaching if w != name) or "os.replace(" in body:
                reaching.add(name)
                changed = True
    return frozenset(reaching)


def _dashboard_source() -> str:
    """The file these derivations read, and the key their caches are keyed on.

    A seventh review round: they were `@lru_cache(maxsize=1)` over no arguments,
    which is a cache that cannot notice its input changing -- the same shape this
    file keeps finding in its own tests, this time in the machinery that finds it.
    Every fix in this spec since round four has been verified by editing
    `dashboard.py` and re-running, so a derivation that answers from before the edit
    would quietly make a mutation look caught, or look missed.

    436: dashboard.py became a package, so the "source" is every module under it,
    concatenated in a stable order. The derivations only walk top-level defs, so
    the concatenation is faithful.
    """
    root = DASHBOARD
    return "\n\n".join(
        path.read_text() for path in sorted(root.rglob("*.py"))
    )


@lru_cache(maxsize=4)
def _reaching_a_writer_for(source: str) -> frozenset[str]:
    return _compute_reaching_a_writer(source)


@lru_cache(maxsize=4)
def _guarded_write_handlers_for(source: str) -> frozenset[str]:
    return _compute_guarded_write_handlers(source)


def _writes_state(target) -> bool:
    """Does this assignment target write the daemon's shared state?

    Three shapes, because a sixth review round found the check knew one:
    ``daemon.state["x"] = y`` (Subscript over the attribute), ``daemon.state = {...}``
    (the attribute itself -- a refactor away, and it replaces the whole dict), and
    ``daemon.state["a"]["b"] = y`` (Subscript over a Subscript, which is a write to
    shared state just as much as the flat form).
    """
    if isinstance(target, ast.Attribute) and target.attr == "state":
        return True
    while isinstance(target, ast.Subscript):
        target = target.value
        if isinstance(target, ast.Attribute) and target.attr == "state":
            return True
    return False


def _guarded_write_handlers() -> frozenset[str]:
    return _guarded_write_handlers_for(_dashboard_source())


def _compute_guarded_write_handlers(source: str) -> frozenset[str]:
    """The handler names the ordering test must inspect, derived from the routes.

    Listing them was the defect: the list could not notice a sixth handler.
    """
    tree = ast.parse(source)
    writers = {
        fn
        for _route, fn, guarded in (
            TestEveryConfigWritingRouteChecksAVersion._routes_reaching_a_writer()
        )
        if guarded
    }
    # Narrowed to the handlers guarded by the header vehicle. The 081 catalog and
    # routing routes take their version in the body and never call _version_refusal,
    # so demanding one of them would assert about a guard they do not use; a new
    # handler with no guard at all is the enumeration test's job, not this one.
    return frozenset(
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name in writers
        and "_version_refusal(" in ast.unparse(node)
    )


def _client(config_path: Path) -> TestClient:
    raw = yaml.safe_load(config_path.read_text())
    raw["github_token"] = "ghp_fixturetoken"
    # A second symphony, because deleting the last one is refused with 409 before the
    # version check is reached — correctly, but it would leave the delete cases here
    # asserting that validation works rather than that the guard does.
    syms = raw.get("symphonies") or []
    if len(syms) == 1:
        spare = {**syms[0], "name": "spare", "github_project_number": 99}
        raw["symphonies"] = [*syms, spare]
    config_path.write_text(yaml.safe_dump(raw, sort_keys=False))
    cfg = CoordinareConfiguration(**coerce_multi_symphony_raw(raw))
    daemon = MagicMock()
    daemon.state = {
        "coordinare_config": cfg,
        "config_version": 7,
        # The symphony routes read this, not coordinare_config.symphonies — without it
        # a delete 404s before it ever reaches the version check, and the test would
        # be asserting nothing.
        "symphony_configs": {s.name: s for s in cfg.symphonies},
        "symphony_states": {s.name: {} for s in cfg.symphonies},
    }
    daemon.running = True
    daemon._cycle_active = False
    app = create_dashboard_app(
        DashboardStore(), daemon, MagicMock(), MagicMock(), config_path=config_path,
    )
    client = TestClient(app, base_url="http://127.0.0.1:8090")
    # The daemon the app closed over, so a test can assert on the in-memory state a
    # refused write must not have touched. Reaching it via gc, as a throwaway probe
    # did, picked up a different MagicMock and reported a real defect as passing.
    client.daemon = daemon
    return client


class TestEveryConfigWritingRouteChecksAVersion:
    """SC-001 — asserted by enumeration, so a route added later is caught."""

    @staticmethod
    def _routes_reaching_a_writer() -> list[tuple[str, str, bool]]:
        tree = ast.parse(_dashboard_source())
        found: list[tuple[str, str, bool]] = []
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            route = None
            for dec in node.decorator_list:
                if (
                    isinstance(dec, ast.Call)
                    and isinstance(dec.func, ast.Attribute)
                    and dec.func.attr in {"post", "put", "delete", "patch"}
                    and dec.args
                    and isinstance(dec.args[0], ast.Constant)
                ):
                    route = f"{dec.func.attr.upper()} {dec.args[0].value}"
            if route is None:
                continue
            body = ast.unparse(node)
            writes = any(f"{w}(" in body for w in _reaching_a_writer()) or (
                "os.replace(" in body
            )
            if not writes:
                continue
            guarded = any(
                token in body
                for token in ("_version_refusal", "expected_hash", "base_hash", "guard_concurrency")
            )
            found.append((route, node.name, guarded))
        return found

    def test_the_enumeration_finds_the_writers_at_all(self) -> None:
        """A test that silently matched nothing would pass forever."""
        routes = self._routes_reaching_a_writer()

        assert len(routes) >= 7, f"only found {len(routes)}; the scan has stopped working"

    def test_none_of_them_writes_without_a_version(self) -> None:
        unguarded = [r for r, _fn, guarded in self._routes_reaching_a_writer() if not guarded]

        assert not unguarded, (
            f"these routes write config.yaml with no version check: {unguarded}. "
            "A half-guarded surface is worse than an unguarded one — an operator who "
            "meets the strict endpoint concludes the surface is strict."
        )


class TestTheFiveRoutesRefuseAndPreserve:
    """FR-001..FR-004, exercised rather than read."""

    @staticmethod
    def _send(client, method: str, url: str, payload: dict, headers=None):
        """DELETE carries no body in httpx's client, which is the reason these
        routes take the version in a header rather than a body field."""
        kwargs = {"headers": headers} if headers else {}
        if method != "delete":
            kwargs["json"] = payload
        return getattr(client, method)(url, **kwargs)

    CASES: tuple[tuple[str, str, dict], ...] = (
        ("post", "/api/symphonies", {"name": "delta", "github_project_number": 44}),
        ("put", "/api/symphonies/demo", {"enabled": False}),
        ("delete", "/api/symphonies/demo", {}),
        ("put", "/api/personas/implementer", {"instructions": "Be brief."}),
        ("delete", "/api/personas/implementer", {}),
    )

    @pytest.mark.parametrize(("method", "url", "payload"), CASES)
    def test_no_version_is_refused_and_nothing_is_written(
        self, temp_config_path, method, url, payload,
    ) -> None:
        client = _client(temp_config_path)
        before = temp_config_path.read_bytes()

        resp = self._send(client, method, url, payload)

        assert resp.status_code == 428, f"{method.upper()} {url}: {resp.text}"
        assert temp_config_path.read_bytes() == before

    @pytest.mark.parametrize(("method", "url", "payload"), CASES)
    def test_a_stale_version_is_refused_and_nothing_is_written(
        self, temp_config_path, method, url, payload,
    ) -> None:
        client = _client(temp_config_path)
        stale = compute_content_hash(temp_config_path)
        temp_config_path.write_text(temp_config_path.read_text() + "\n# meanwhile\n")
        before = temp_config_path.read_bytes()

        resp = self._send(client, method, url, payload, headers={"If-Match": stale})

        assert resp.status_code == 409, f"{method.upper()} {url}: {resp.text}"
        assert temp_config_path.read_bytes() == before
        assert "# meanwhile" in temp_config_path.read_text()

    def test_a_refused_delete_leaves_the_symphony_in_memory_too(
        self, temp_config_path,
    ) -> None:
        """SC-003 — the file is not the only state this handler mutates.

        A refusal that had already dropped the symphony from memory would be a worse
        outcome than the overwrite it prevented, so the check sits before the
        mutation rather than merely before the write.
        """
        client = _client(temp_config_path)
        before = client.get("/api/symphonies").json()

        resp = client.delete("/api/symphonies/demo")

        assert resp.status_code == 428
        assert client.get("/api/symphonies").json() == before


class TestTheRefusalIsUsable:
    """FR-002 — 'precondition required' with no remedy is a worse error than none."""

    def test_it_names_the_header_and_where_to_get_it(self, temp_config_path) -> None:
        client = _client(temp_config_path)

        body = client.put("/api/personas/implementer", json={"instructions": "x"}).json()

        assert body["precondition_required"] is True
        assert "If-Match" in body["error"]
        assert "ETag" in body["error"]


class TestNothingElseChanged:
    """SC-004 / FR-007."""

    def test_the_global_write_still_takes_its_body_field(self, temp_config_path) -> None:
        client = _client(temp_config_path)
        current = compute_content_hash(temp_config_path)

        resp = client.put(
            "/api/config/global",
            json={"max_concurrent_cards": 4, "expected_hash": current},
        )

        assert resp.status_code == 200, resp.text

    def test_the_global_write_now_also_accepts_if_match(self, temp_config_path) -> None:
        """FR-007 — one mechanism to document going forward, nothing existing broken."""
        client = _client(temp_config_path)
        current = compute_content_hash(temp_config_path)

        resp = client.put(
            "/api/config/global",
            json={"max_concurrent_cards": 4},
            headers={"If-Match": current},
        )

        assert resp.status_code == 200, resp.text

    def test_validation_still_runs_before_the_version_check(self, temp_config_path) -> None:
        """Placement matters: at the top of the handler this returned 428 for an
        unknown role, asking for a version to do something that was never going to
        happen."""
        client = _client(temp_config_path)

        resp = client.put("/api/personas/wizard", json={"instructions": "x"})

        assert resp.status_code == 404, "the version check jumped ahead of validation"

    def test_a_deployment_with_no_temp_config_path_is_unaffected(self, tmp_path) -> None:
        """The writers already no-op with no file, so demanding a version would
        refuse a write that could never happen — and there would be no version to
        give, since the version *is* the file."""
        daemon = MagicMock()
        daemon.state = {"coordinare_config": None, "config_version": 0}
        daemon._cycle_active = False
        app = create_dashboard_app(DashboardStore(), daemon, MagicMock(), MagicMock())
        client = TestClient(app, base_url="http://127.0.0.1:8090")

        resp = client.put("/api/symphonies/demo", json={"enabled": False})

        assert resp.status_code != 428, "refused a write that had no file to overwrite"


def test_the_helper_exists_once_rather_than_five_times() -> None:
    """FR-008 — five copies of a concurrency check is five chances to differ, and
    the difference shows up as a lost edit, which is the failure nobody notices."""
    source = _dashboard_source()

    assert source.count("def _version_refusal(") == 1
    assert len(re.findall(r"_version_refusal\(config_path", source)) >= 5


def _js(text: str) -> str:
    """The dashboard's inlined front-end, such as it is: one big string constant."""
    return text


#: How far back to look for the version a call sends. 157's global-config save builds
#: `body.expected_hash = baseHash` a couple of statements before its fetch, so a check
#: confined to the call itself calls a correct call site broken. Measured rather than
#: guessed: the only two sites that need the window sit 34 and 56 characters back, so
#: this is roughly triple the real requirement and no more. It was 1200 first, which
#: worked and left a wide gap in which an unrelated mention of a version could mask an
#: unguarded call next to it.
_LOOKBACK = 200

#: Not every write goes through `fetch` directly. The 081 config page routes its
#: section, catalog and routing saves through this wrapper, which takes the URL as a
#: variable -- so the fetch inside it has no URL literals to match and the six call
#: sites around it were invisible to the cross-reference. They are all guarded, by a
#: `base_hash` in the payload; the point of naming the wrapper is that an unguarded
#: seventh would now be caught rather than passing because of how it was written.
_WRITE_WRAPPERS = ("cfgPut",)


_JS_STRING = re.compile(r"'([^']*)'|\"([^\"]*)\"|`([^`]*)`")


def _js_literals(expr: str) -> str:
    """The string literals in a JS expression, joined, whatever quotes they use.

    A sixth review round: this was ``re.findall(r"'([^']*)'", expr)``. Every URL in
    this file happens to be single-quoted, so the regex worked -- and a refactor to
    double quotes or a template literal would have produced *no literals*, silently
    dropping that route from the cross-reference below rather than failing it. The
    test would have gone on passing with the version gone from the call.

    A template literal's ``${...}`` holes come out, the way ``_route_skeleton``
    removes ``{role}`` from the server path, so the two sides still correspond.
    """
    parts: list[str] = []
    for match in _JS_STRING.finditer(expr):
        single, double, tick = match.groups()
        if tick is not None:
            parts.append(re.sub(r"\$\{[^}]*\}", "", tick))
        else:
            parts.append(single if single is not None else double)
    return "".join(parts)


def _js_method(call: str) -> str | None:
    """The HTTP verb a fetch options object names, in any quote style."""
    match = re.search(r"method:\s*['\"`](POST|PUT|DELETE|PATCH)['\"`]", call)
    return match.group(1) if match else None


def _fetch_calls(text: str) -> list[tuple[str, str, str]]:
    """Every call to fetch or a write wrapper, as (callee, args, preceding context).

    A regex cannot find the call: the options object contains braces and the URL is
    built by concatenation. Bracket matching is crude but it reads the whole call,
    which is what makes the assertion below about the call rather than a nearby line.
    The callee comes back too, because a wrapper call is identified by its name and
    guessing from the argument text instead classified plain GETs as writes.
    """
    out: list[tuple[str, str, str]] = []
    pattern = "|".join(("fetch", *_WRITE_WRAPPERS))
    for match in re.finditer(rf"\b({pattern})\s*\(", text):
        start = match.end() - 1
        depth = 0
        for index in range(start, min(start + 4000, len(text))):
            char = text[index]
            if char in "([{":
                depth += 1
            elif char in ")]}":
                depth -= 1
                if depth == 0:
                    out.append(
                        (
                            match.group(1),
                            text[start + 1 : index],
                            text[max(0, start - _LOOKBACK) : start],
                        ),
                    )
                    break
    return out


def _first_arg(call: str) -> str:
    depth = 0
    for index, char in enumerate(call):
        if char in "([{":
            depth += 1
        elif char in ")]}":
            depth -= 1
        elif char == "," and depth == 0:
            return call[:index]
    return call


def _route_skeleton(path: str) -> str:
    """``/api/personas/{role}`` -> ``/api/personas/``.

    The JS builds the same path by concatenating literals around
    ``encodeURIComponent(role)``, so joining its string literals produces exactly
    this. That correspondence is what lets the two sides be compared at all.
    """
    return re.sub(r"\{[^}]*\}", "", path)


class TestTheVersionIsObtainable:
    """FR-005 — a guard demanding a version the client cannot get is an outage.

    The 428 body tells the caller that "the matching GET returns the current version
    as an ETag". That sentence was false for all five routes when it was written.
    """

    @pytest.mark.parametrize("url", ["/api/symphonies", "/api/symphonies/demo", "/api/personas"])
    def test_the_get_a_page_loads_from_carries_the_version(self, temp_config_path, url) -> None:
        client = _client(temp_config_path)

        res = client.get(url)

        assert res.status_code == 200
        etag = res.headers.get("ETag")
        assert etag, f"GET {url} returns no ETag, so nothing can satisfy the write guard"
        # RFC 7232 §2.3: an entity-tag is DQUOTE-enclosed. _version_refusal strips
        # them back off, so an unquoted tag would work and still be a malformed
        # header -- the kind of thing that works until a proxy starts caring.
        assert etag.startswith('"') and etag.endswith('"'), etag
        assert compute_content_hash(temp_config_path) == etag.strip('"')


class TestTheClientActuallySendsIt:
    """The regression this branch shipped before review caught it.

    Every one of the five guarded routes has a front-end caller, and not one of them
    sent an If-Match. The suite was green because the tests threaded the header in
    themselves. Cross-referencing the two sides is the only thing that notices.
    """

    @staticmethod
    def _write_fetches() -> list[tuple[str, str, bool]]:
        source = _frontend_source()
        rows: list[tuple[str, str, bool]] = []
        for callee, call, before in _fetch_calls(source):
            method = _js_method(call)
            # A wrapper call is a write by construction; a bare fetch is one only if
            # it names a mutating method. Without the callee this had to be guessed
            # from the argument text, which classified every GET as a write.
            if not method and callee not in _WRITE_WRAPPERS:
                continue
            url_expr = _first_arg(call)
            literals = _js_literals(url_expr)
            # Two vehicles carry a version, and the assertion is about the version
            # rather than the header: 157's global write takes `expected_hash` in the
            # body and its callers were written that way, so it accepts both.
            # A wrapper takes its version in the payload it is handed, so the token
            # must be IN the call. Allowing the lookback here let a neighbouring
            # save's `base_hash` stand in for a missing one -- mutation-tested: with
            # the window, deleting the section save's version was not caught.
            haystack = call if callee in _WRITE_WRAPPERS else call + before
            sends = any(
                token in haystack for token in ("If-Match", "expected_hash", "base_hash")
            )
            # `cfgPut` reads its method from the payload, so a wrapper call has no
            # verb to match on and is keyed by path alone. Keying it "WRITE" instead
            # left three guarded routes with no matched caller, and a version removed
            # from any of them would have gone unnoticed.
            verb = method or "*"
            rows.append((f"{verb} {literals}", call, sends))
        return rows

    def test_every_guarded_route_has_a_caller_that_sends_a_version(self) -> None:
        guarded = {
            f"{route.split(' ', 1)[0]} {_route_skeleton(route.split(' ', 1)[1])}"
            for route, _fn, is_guarded in (
                TestEveryConfigWritingRouteChecksAVersion._routes_reaching_a_writer()
            )
            if is_guarded
        }
        fetches = self._write_fetches()

        guarded_paths = {key.split(" ", 1)[1] for key in guarded}
        missing = [
            key
            for key, _call, sends in fetches
            if not sends
            and (
                key in guarded
                or (key.startswith("* ") and key.split(" ", 1)[1] in guarded_paths)
            )
        ]
        assert not missing, (
            f"these front-end calls hit a version-guarded route without If-Match: "
            f"{sorted(set(missing))}. Each one is a UI action that now fails with 428."
        )

    def test_the_cross_reference_is_not_matching_nothing(self) -> None:
        """The assertion above passes trivially if the two sides never line up."""
        guarded = {
            f"{route.split(' ', 1)[0]} {_route_skeleton(route.split(' ', 1)[1])}"
            for route, _fn, is_guarded in (
                TestEveryConfigWritingRouteChecksAVersion._routes_reaching_a_writer()
            )
            if is_guarded
        }
        guarded_paths = {key.split(" ", 1)[1] for key in guarded}
        matched = [
            key
            for key, _call, _sends in self._write_fetches()
            if key in guarded
            or (key.startswith("* ") and key.split(" ", 1)[1] in guarded_paths)
        ]

        assert len(matched) >= 13, (
            f"only {len(matched)} front-end write calls matched a guarded route; "
            f"the skeleton correspondence has broken and this test proves nothing"
        )

    def test_the_action_routes_are_left_alone(self) -> None:
        """env-bootstrap and wiki-init are actions, not config writes.

        They must NOT have grown an If-Match, or the header has been sprayed at
        everything and the test above stops meaning anything.
        """
        sprayed = [
            key
            for key, _call, sends in self._write_fetches()
            if sends and ("env-bootstrap" in key or "wiki-init" in key or "validate" in key)
        ]

        assert not sprayed, f"If-Match sent to routes that do not guard on it: {sprayed}"


class TestASecondSaveInTheSameSessionWorks:
    """FR-006 — the bug this project has now shipped twice.

    156 left ``_adminCfgHash = null`` after a save, so the guard held exactly once per
    page load; 157's Apply button read whatever hash another page had left behind. A
    write that does not hand back the new version has the same defect by omission.
    """

    def test_each_write_returns_the_post_write_version(self, temp_config_path) -> None:
        client = _client(temp_config_path)
        before = client.get("/api/personas").headers["ETag"]

        first = client.put(
            "/api/personas/implementer",
            json={"instructions": "Be brief."},
            headers={"If-Match": before},
        )

        assert first.status_code == 200
        after = first.headers.get("ETag")
        assert after, "the write returns no new version, so the next one cannot succeed"
        assert after != before, "the version did not change across a write that changed the file"
        assert after.strip('"') == compute_content_hash(temp_config_path)

    def test_the_returned_version_is_accepted_by_the_next_write(self, temp_config_path) -> None:
        client = _client(temp_config_path)
        version = client.get("/api/personas").headers["ETag"]

        first = client.put(
            "/api/personas/implementer",
            json={"instructions": "First."},
            headers={"If-Match": version},
        )
        assert first.status_code == 200

        second = client.put(
            "/api/personas/implementer",
            json={"instructions": "Second."},
            headers={"If-Match": first.headers["ETag"]},
        )

        assert second.status_code == 200, (
            f"the second save in a session was refused ({second.status_code}); the "
            f"version a write hands back is not one it will accept"
        )
        assert second.json()["instructions"] == "Second."

    def test_the_stale_version_is_still_refused(self, temp_config_path) -> None:
        """The refresh must not have been implemented by dropping the check."""
        client = _client(temp_config_path)
        stale = client.get("/api/personas").headers["ETag"]

        assert (
            client.put(
                "/api/personas/implementer",
                json={"instructions": "First."},
                headers={"If-Match": stale},
            ).status_code
            == 200
        )
        again = client.put(
            "/api/personas/implementer",
            json={"instructions": "Second."},
            headers={"If-Match": stale},
        )

        assert again.status_code == 409, "a stale version was accepted"


class TestTheTwoKindsOf409AreDistinguishable:
    """409 is not only ours.

    'Symphony already exists', 'Cannot delete the last symphony' and 'A cycle is in
    progress' are all 409s on these same routes. The front-end relabels a version
    conflict as "Config changed since this page loaded. Reload and re-apply." -- which
    would send an operator chasing a concurrent edit that never happened if it were
    shown for a duplicate name. `conflict: true` is what separates them, so it has to
    mean only one thing.
    """

    def test_a_stale_version_says_conflict(self, temp_config_path) -> None:
        client = _client(temp_config_path)
        stale = compute_content_hash(temp_config_path)
        temp_config_path.write_text(temp_config_path.read_text() + "\n# meanwhile\n")

        res = client.put(
            "/api/personas/implementer",
            json={"instructions": "Be brief."},
            headers={"If-Match": stale},
        )

        assert res.status_code == 409
        assert res.json().get("conflict") is True

    def test_a_duplicate_name_does_not(self, temp_config_path) -> None:
        client = _client(temp_config_path)

        res = client.post(
            "/api/symphonies",
            json={"name": "demo", "github_project_number": 44},
            headers={"If-Match": compute_content_hash(temp_config_path)},
        )

        assert res.status_code == 409
        body = res.json()
        assert not body.get("conflict"), (
            "a name collision is flagged as a version conflict, so the UI will tell the "
            "operator to reload over an error a reload cannot fix"
        )
        assert "already exists" in body["error"]

    def test_deleting_the_last_symphony_does_not(self, temp_config_path) -> None:
        raw = yaml.safe_load(temp_config_path.read_text())
        raw["github_token"] = "ghp_fixturetoken"
        cfg = CoordinareConfiguration(**coerce_multi_symphony_raw(raw))
        daemon = MagicMock()
        daemon.state = {
            "coordinare_config": cfg,
            "config_version": 7,
            "symphony_configs": {s.name: s for s in cfg.symphonies},
            "symphony_states": {s.name: {} for s in cfg.symphonies},
        }
        daemon.running = True
        daemon._cycle_active = False
        client = TestClient(
            create_dashboard_app(
                DashboardStore(), daemon, MagicMock(), MagicMock(), config_path=temp_config_path,
            ),
            base_url="http://127.0.0.1:8090",
        )

        res = client.delete(
            "/api/symphonies/demo", headers={"If-Match": compute_content_hash(temp_config_path)},
        )

        assert res.status_code == 409
        assert not res.json().get("conflict")

    def test_nothing_else_in_the_file_claims_conflict(self) -> None:
        """The flag is set in exactly the two places that mean it."""
        source = _dashboard_source()

        assert source.count('"conflict": True') == 2, (
            "a new 409 has claimed to be a version conflict; the UI will mislabel it"
        )


class TestARefusalTouchesNoStateAtAll:
    """SC-007 — the file is not the only thing these handlers write.

    This existed for DELETE only, because that is where the bug was found, and the
    parametrized refusal tests above compare file bytes. Between them they missed
    that POST and PUT mutate ``daemon.state`` before the guard runs: a 428 left the
    symphony added or updated in memory with ``config_version`` bumped, so the
    dashboard showed a change that was never persisted and the next reload silently
    undid it. Checking one handler for a defect found in that handler is how the
    other two stayed broken through four commits and two review rounds.
    """

    CASES: tuple[tuple[str, str, dict], ...] = (
        ("post", "/api/symphonies", {"name": "gamma", "github_project_number": 30}),
        ("put", "/api/symphonies/demo", {"enabled": False}),
        ("delete", "/api/symphonies/demo", {}),
        ("put", "/api/personas/implementer", {"instructions": "Be brief."}),
        ("delete", "/api/personas/implementer", {}),
    )

    @staticmethod
    def _snapshot(daemon) -> dict:
        """Names AND values: `configs[name] = updated` keeps the key set identical,
        so a comparison of names alone reports the PUT case as clean."""
        configs = daemon.state.get("symphony_configs") or {}
        return {
            "version": daemon.state.get("config_version"),
            "names": sorted(configs),
            "values": {
                n: (getattr(c, "enabled", None), getattr(c, "github_project_number", None))
                for n, c in configs.items()
            },
        }

    @pytest.mark.parametrize(("method", "url", "payload"), CASES)
    def test_no_version_leaves_memory_untouched(
        self, temp_config_path, method, url, payload,
    ) -> None:
        client = _client(temp_config_path)
        before = self._snapshot(client.daemon)

        resp = TestTheFiveRoutesRefuseAndPreserve._send(client, method, url, payload)

        assert resp.status_code == 428, f"{method.upper()} {url}: {resp.text}"
        assert self._snapshot(client.daemon) == before, (
            f"{method.upper()} {url} was refused with 428 and still changed in-memory "
            f"state. The dashboard now shows a change that was never written."
        )

    @pytest.mark.parametrize(("method", "url", "payload"), CASES)
    def test_a_stale_version_leaves_memory_untouched(
        self, temp_config_path, method, url, payload,
    ) -> None:
        client = _client(temp_config_path)
        stale = compute_content_hash(temp_config_path)
        temp_config_path.write_text(temp_config_path.read_text() + "\n# meanwhile\n")
        before = self._snapshot(client.daemon)

        resp = TestTheFiveRoutesRefuseAndPreserve._send(
            client, method, url, payload, headers={"If-Match": stale},
        )

        assert resp.status_code == 409, f"{method.upper()} {url}: {resp.text}"
        assert self._snapshot(client.daemon) == before, (
            f"{method.upper()} {url} lost the race and still changed in-memory state"
        )

    def test_the_derived_handler_set_still_finds_them(self) -> None:
        """The set is derived now, and a derivation that matches nothing passes."""
        handlers = _guarded_write_handlers()

        assert {"create_symphony", "update_symphony", "delete_symphony"} <= handlers, (
            f"the derivation stopped finding the symphony handlers: {sorted(handlers)}"
        )

    def test_the_guard_sits_above_every_mutation(self) -> None:
        """Asserted structurally as well, so the ordering cannot drift back.

        Reading the file is what found this; a test that only sends requests would
        pass again the moment someone reorders a handler in a way the five cases
        above happen not to cover.
        """
        import ast

        tree = ast.parse(_dashboard_source())
        handlers = _guarded_write_handlers()
        offenders: list[str] = []
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            # Derived, not listed. A sixth review round: the literal set
            # {create_symphony, update_symphony, delete_symphony} meant a new
            # symphony handler added tomorrow was skipped in silence -- the same
            # inventory-that-cannot-notice-itself-change shape this file has now
            # found four times. Any guarded route reaching a writer qualifies;
            # handlers that touch no daemon.state simply have nothing to offend.
            if node.name not in handlers:
                continue
            guard_line = next(
                (
                    n.lineno
                    for n in ast.walk(node)
                    if isinstance(n, ast.Call)
                    and isinstance(n.func, ast.Name)
                    and n.func.id == "_version_refusal"
                ),
                None,
            )
            assert guard_line, f"{node.name} has no version guard at all"
            # Subscript assignment is how these handlers happen to mutate state today.
            # Checking only that form was the defect a fourth review round found: a
            # `daemon.state.update({...})` above the guard would have passed, and so
            # would `.pop`, `setdefault`, `del`, or an augmented assignment.
            mutators = {"update", "pop", "setdefault", "clear", "popitem"}
            for n in ast.walk(node):
                if getattr(n, "lineno", guard_line) >= guard_line:
                    continue
                hit = False
                if isinstance(n, (ast.Assign, ast.AugAssign)):
                    targets = n.targets if isinstance(n, ast.Assign) else [n.target]
                    hit = any(_writes_state(t) for t in targets)
                elif isinstance(n, ast.Delete):
                    hit = any(_writes_state(t) for t in n.targets)
                elif isinstance(n, ast.Call):
                    hit = (
                        isinstance(n.func, ast.Attribute)
                        and n.func.attr in mutators
                        and isinstance(n.func.value, ast.Attribute)
                        and n.func.value.attr == "state"
                    )
                if hit:
                    offenders.append(f"{node.name}:{n.lineno} (guard at {guard_line})")
        assert not offenders, (
            f"these handlers write daemon.state before checking the version: {offenders}. "
            "A refusal would leave that write applied."
        )


class TestAConflictIsReportedUsefully:
    """FR-011 across every call site, not the ones that came to mind.

    The helper was added and then wired into three of seven paths. The other four
    showed 'Error 409', 'Error resetting' and 'Error: Conflict' -- a version clash
    reported as a number, which tells an operator nothing about the reload that would
    fix it. One of them did not read the response body at all, so the `conflict` flag
    that distinguishes a version clash from a duplicate name could not be seen.

    Two handlers qualify, because the 081 config page got there first with its own:
    `versionErrorText` for the routes this spec guards, and `cfgHandleSave`, which
    routes 409 to `cfgConflict` and refreshes the page hash from `new_hash`. Either
    is conflict-aware; a bare status code is not.
    """

    #: How far past the call the response handling sits. Measured: the furthest of
    #: these is ~420 characters, and a window much wider would start reading the next
    #: function's error handling as if it belonged to this call.
    _FORWARD = 700

    #: Three ways a call site can be conflict-aware, all of them present in this file
    #: already. Naming them beats naming one and calling the others broken:
    #: `versionErrorText` is 158's helper for the routes this spec guards;
    #: `cfgHandleSave` is 081's, which routes 409 to `cfgConflict` and refreshes the
    #: page hash from `new_hash`; and 156/157's two global-config saves branch on the
    #: status inline with their own message. What is NOT acceptable is a call site that
    #: reports an HTTP error without distinguishing 409 at all.
    CONFLICT_AWARE = ("versionErrorText", "cfgHandleSave", "status === 409")

    @staticmethod
    def _guarded_write_sites() -> list[tuple[str, str]]:
        source = _frontend_source()
        guarded = {
            f"{route.split(' ', 1)[0]} {_route_skeleton(route.split(' ', 1)[1])}"
            for route, _fn, is_guarded in (
                TestEveryConfigWritingRouteChecksAVersion._routes_reaching_a_writer()
            )
            if is_guarded
        }
        guarded_paths = {key.split(" ", 1)[1] for key in guarded}
        sites: list[tuple[str, str]] = []
        for callee, call, _before in _fetch_calls(source):
            method = _js_method(call)
            if not method and callee not in _WRITE_WRAPPERS:
                continue
            literals = _js_literals(_first_arg(call))
            key = f"{method or '*'} {literals}"
            if not (
                key in guarded
                or (key.startswith("* ") and key.split(" ", 1)[1] in guarded_paths)
            ):
                continue
            start = source.find(call) + len(call)
            following = source[start : start + TestAConflictIsReportedUsefully._FORWARD]
            if callee in _WRITE_WRAPPERS:
                # A wrapper call's response handling lives inside the wrapper, not
                # after the call, so that is where to look for it.
                wrapper = re.search(
                    rf"function\s+{re.escape(callee)}\s*\(.*?\n\}}", source, re.DOTALL,
                )
                following += wrapper.group(0) if wrapper else ""
            sites.append((key, following))
        return sites

    def test_every_guarded_write_reports_a_conflict_actionably(self) -> None:
        sites = self._guarded_write_sites()

        bare = [
            key
            for key, following in sites
            if not any(handler in following for handler in self.CONFLICT_AWARE)
        ]

        assert not bare, (
            f"these guarded writes display an HTTP error without a conflict-aware "
            f"handler: {sorted(set(bare))}. A 409 there reads as a number, and the "
            f"operator has no way to know a reload fixes it."
        )

    def test_the_scan_found_the_sites(self) -> None:
        assert len(self._guarded_write_sites()) >= 13

    def test_the_body_is_read_wherever_the_helper_is_used(self) -> None:
        """`versionErrorText(status, null)` cannot see the flag it keys on.

        This is how the persona save reported every 409 as 'Error 409' while looking
        entirely correct -- the call was there, the argument was not.
        """
        source = _frontend_source()

        null_bodied = re.findall(r"versionErrorText\(\s*[^,]+,\s*null\s*\)", source)

        assert not null_bodied, (
            f"versionErrorText called with a null body: {null_bodied}. It keys on "
            f"`body.conflict`, so this silently degrades to the generic branch."
        )


class TestAnUnreadableConfigIsNotACrash:
    """FR-012 — ``guard_concurrency`` propagates OSError on purpose.

    Its docstring is explicit: ``compute_content_hash`` returns the empty-file
    sentinel for a present-but-unreadable file, so a bare hash comparison would let
    a sentinel baseline match and clobber data the UI never read. It re-probes and
    lets OSError out "so callers degrade to a structured forbidden result rather
    than proceeding". Catching only ConcurrencyConflictError turned that into a 500
    with a traceback -- the guard was written against half the contract.

    403 and the message match ``_conflict_result``, which the 081 writes have used
    for this since spec 081.
    """

    @staticmethod
    def _make_unreadable(path: Path) -> bool:
        """True if the file is genuinely unreadable now. Root ignores the mode."""
        path.chmod(0o000)
        try:
            path.read_bytes()
        except OSError:
            return True
        return False

    @pytest.mark.parametrize(
        ("method", "url", "payload"), TestTheFiveRoutesRefuseAndPreserve.CASES,
    )
    def test_a_present_but_unreadable_config_is_403(
        self, temp_config_path, method, url, payload,
    ) -> None:
        client = _client(temp_config_path)
        version = compute_content_hash(temp_config_path)
        if not self._make_unreadable(temp_config_path):
            temp_config_path.chmod(0o644)
            pytest.skip("cannot make a file unreadable as this user")
        try:
            resp = TestTheFiveRoutesRefuseAndPreserve._send(
                client, method, url, payload, headers={"If-Match": version},
            )
        finally:
            temp_config_path.chmod(0o644)

        assert resp.status_code == 403, (
            f"{method.upper()} {url}: expected 403, got {resp.status_code}. A 500 here "
            f"is an unhandled OSError from guard_concurrency."
        )
        message = resp.json()["error"]
        assert "Could not read the configuration file" in message
        # `str(exc)` on an OSError carries the absolute path, and this body reaches the
        # browser. The first version of this fix interpolated the exception whole --
        # copied from _conflict_result, which had the same leak.
        assert str(temp_config_path) not in message, (
            f"the 403 body leaks the config path: {message!r}"
        )
        assert temp_config_path.name not in message, (
            f"the 403 body leaks the config filename: {message!r}"
        )

    def test_the_global_write_too(self, temp_config_path) -> None:
        client = _client(temp_config_path)
        version = compute_content_hash(temp_config_path)
        if not self._make_unreadable(temp_config_path):
            temp_config_path.chmod(0o644)
            pytest.skip("cannot make a file unreadable as this user")
        try:
            resp = client.put(
                "/api/config/global",
                json={"max_concurrent_cards": 4, "expected_hash": version},
            )
        finally:
            temp_config_path.chmod(0o644)

        assert resp.status_code == 403, (
            f"expected 403, got {resp.status_code}: spec 157's endpoint has the same "
            f"half-caught contract"
        )
        assert str(temp_config_path) not in resp.json()["error"]

    def test_both_guards_catch_it(self) -> None:
        """Structural, so a third guard added later cannot forget.

        Every ``guard_concurrency`` call in the dashboard must handle OSError.
        """
        import ast

        tree = ast.parse(_dashboard_source())
        unhandled: list[int] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Try):
                continue
            calls = [
                n
                for n in ast.walk(node)
                if isinstance(n, ast.Call)
                and isinstance(n.func, ast.Name)
                and n.func.id == "guard_concurrency"
            ]
            if not calls:
                continue
            caught = {
                h.type.id
                for h in node.handlers
                if isinstance(h.type, ast.Name)
            }
            if "OSError" not in caught:
                unhandled.append(calls[0].lineno)

        assert not unhandled, (
            f"guard_concurrency called without handling OSError at lines {unhandled}. "
            f"It propagates OSError deliberately; an uncaught one is a 500."
        )


class TestNo403LeaksThePath:
    """The 403 added for FR-015 told the caller where the config file lives.

    `str(exc)` on an OSError is "[Errno 13] Permission denied: '/etc/coordinare/
    config.yaml'", and that body goes to the browser. `strerror` is the half that
    helps without the half that does not.

    Worth its own class because of where it came from: 158 copied this message from
    `_conflict_result` deliberately, for consistency with the 081 writes, and copied
    its leak. The original had it too, since spec 081. Consistency is not a
    substitute for reading the thing being copied.
    """

    def test_the_081_writes_do_not_leak_it_either(self, tmp_path) -> None:
        from coordinare.services.config_write_service import _conflict_result

        target = tmp_path / "secret-location.yaml"
        target.write_text("poll_interval_seconds: 60\n")
        baseline = compute_content_hash(target)
        target.chmod(0o000)
        try:
            if target.is_file():
                try:
                    target.read_bytes()
                    pytest.skip("cannot make a file unreadable as this user")
                except OSError:
                    pass
            result = _conflict_result(target, baseline)
        finally:
            target.chmod(0o644)

        assert result is not None and not result.ok
        message = result.errors[0].message
        assert result.errors[0].code == "forbidden"
        assert "secret-location" not in message, f"path leaked: {message!r}"
        assert str(tmp_path) not in message, f"path leaked: {message!r}"

    def test_no_handler_interpolates_a_bare_oserror_into_a_body(self) -> None:
        """Structural, because this is a shape rather than one line.

        Any `{exc}` inside a response body where `exc` is the caught OSError puts the
        path on the wire. `exc.strerror` is the safe form.
        """
        import ast

        offenders: list[str] = []
        sources = [
            *sorted(DASHBOARD.rglob("*.py")),
            Path("src/coordinare/services/config_write_service.py"),
        ]
        for path in sources:
            tree = ast.parse(path.read_text())
            for handler in (n for n in ast.walk(tree) if isinstance(n, ast.ExceptHandler)):
                name = handler.name
                if not name or not self._catches_oserror(handler):
                    continue
                for node in ast.walk(handler):
                    if self._leaks(node, name, handler):
                        offenders.append(f"{path.name}:{node.lineno}")

        assert not offenders, (
            f"a caught OSError is interpolated whole into a message at {offenders}. "
            f"str(OSError) includes the absolute path; use exc.strerror."
        )

    @staticmethod
    def _mentions(node, name: str) -> bool:
        """Does this expression reach the caught exception at all?"""
        import ast

        return any(
            isinstance(inner, ast.Name) and inner.id == name for inner in ast.walk(node)
        )

    @classmethod
    def _is_safe_field(cls, node, name: str) -> bool:
        """The two ways a message may reach the exception without reaching the path.

        ``exc.strerror`` is the one field with no path in it, and
        ``safe_failure_reason(exc)`` is the helper this spec added to produce exactly
        that answer for the exceptions that have no such field. Everything else that
        touches the exception is a leak until it is named here.
        """
        import ast

        if (
            isinstance(node, ast.Attribute)
            and node.attr == "strerror"
            and isinstance(node.value, ast.Name)
            and node.value.id == name
        ):
            return True
        return (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "safe_failure_reason"
        )

    @classmethod
    def _leaks(cls, node, name: str, handler) -> bool:
        """Does this node put the caught exception's own words into a message?

        Written first for ``{exc}``, then widened to five forms by a fifth review
        round, and widened again by a sixth: each of those was a list of the shapes
        someone had thought of, and ``{exc.args[0]}`` -- which is the message, path
        and all -- was in none of them, because the check demanded an ``ast.Name``
        and that is a ``Subscript``. So does ``repr(exc)``.

        Inverted now: any expression that *reaches* the exception leaks, except the
        one field that is safe. A seventh form of the same mistake has nowhere to
        hide, and adding a genuinely safe accessor means naming it in
        :meth:`_is_safe_field` -- a deliberate act rather than an omission.
        """
        import ast

        # f"...{exc}", f"...{exc!r}", f"...{exc.args[0]}". `{exc.strerror}` is fine.
        if isinstance(node, ast.JoinedStr):
            return any(
                isinstance(value, ast.FormattedValue)
                and cls._mentions(value.value, name)
                and not cls._is_safe_field(value.value, name)
                for value in node.values
            )
        # "..." % exc
        if isinstance(node, ast.BinOp):
            return (
                isinstance(node.op, ast.Mod)
                and cls._mentions(node.right, name)
                and not cls._is_safe_field(node.right, name)
            )
        if not isinstance(node, ast.Call):
            return False
        args_leak = any(
            cls._mentions(arg, name) and not cls._is_safe_field(arg, name)
            for arg in node.args
        )
        if not args_leak:
            return False
        # str(exc) / repr(exc) / format(exc), outside an f-string -- unless it is
        # going to the log, which is exactly where the path belongs.
        if isinstance(node.func, ast.Name) and node.func.id in {"str", "repr", "format"}:
            return not cls._is_log_call(handler, node)
        # "...".format(exc)
        return isinstance(node.func, ast.Attribute) and node.func.attr == "format"

    @staticmethod
    def _is_log_call(handler, target) -> bool:
        """`str(exc)` inside a log call is correct -- the log is where the path
        belongs. Only a response body is a leak."""
        import ast

        for node in ast.walk(handler):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if node.func.attr not in {"warning", "info", "error", "debug", "exception"}:
                continue
            if any(target is inner for inner in ast.walk(node)):
                return True
        return False

    @staticmethod
    def _catches_oserror(handler) -> bool:
        import ast

        names = handler.type
        if names is None:
            return True
        candidates = names.elts if isinstance(names, ast.Tuple) else [names]
        for candidate in candidates:
            if isinstance(candidate, ast.Name) and candidate.id == "OSError":
                return True
            # A yaml.YAMLError's own message carries the path too, as
            # `in "/etc/coordinare/config.yaml", line 3, column 1` -- a second way to
            # leak it that checking OSError alone would have missed.
            if isinstance(candidate, ast.Attribute) and candidate.attr in {
                "YAMLError",
                "MarkedYAMLError",
            }:
                return True
        return False


class TestSafeFailureReason:
    """One implementation, because there were six inline copies and the first two
    fixes were both wrong in a different way.

    The first used `exc.strerror` inline, which crashed with AttributeError on the
    handler that also catches UnicodeDecodeError and yaml.YAMLError -- three existing
    tests caught that. The second missed that a YAMLError's own message carries the
    path as `in "/etc/coordinare/config.yaml", line 3`.
    """

    def test_an_oserror_gives_its_strerror_and_no_path(self, tmp_path) -> None:
        from coordinare.services.config_write_service import safe_failure_reason

        target = tmp_path / "config.yaml"
        target.write_text("a: 1")
        target.chmod(0o000)
        try:
            target.read_bytes()
            pytest.skip("cannot make a file unreadable as this user")
        except OSError as exc:
            reason = safe_failure_reason(exc)
        finally:
            target.chmod(0o644)

        assert reason == "Permission denied"
        assert str(tmp_path) not in reason

    def test_a_yaml_error_does_not_carry_the_path(self, tmp_path) -> None:
        from coordinare.services.config_write_service import safe_failure_reason

        target = tmp_path / "config.yaml"
        target.write_text("a: [unclosed\n")
        try:
            yaml.safe_load(target.read_text())
        except yaml.YAMLError as exc:
            reason = safe_failure_reason(exc)
        else:
            pytest.fail("expected a YAML error")

        assert reason == "the file is not valid YAML"
        # The exception's own words name the file; that is the point of the helper.
        assert str(tmp_path) not in reason

    def test_invalid_utf8_says_so(self) -> None:
        from coordinare.services.config_write_service import safe_failure_reason

        try:
            b"a: \xff".decode("utf-8")
        except UnicodeDecodeError as exc:
            assert safe_failure_reason(exc) == "the file is not valid UTF-8"
        else:
            pytest.fail("expected a decode error")

    def test_it_never_raises_on_an_exception_without_strerror(self) -> None:
        """The AttributeError three existing tests caught, asserted directly."""
        from coordinare.services.config_write_service import safe_failure_reason

        for exc in (ValueError("x"), RuntimeError("y"), yaml.YAMLError("z")):
            assert isinstance(safe_failure_reason(exc), str)


class TestThePersonaValueErrorCarriesNoPath:
    """A fifth review round found a third vector for the path leak.

    Rounds three and four chased `OSError` and then `yaml.YAMLError`. The persona
    handlers return every `ValueError` from `save_persona` verbatim as a 400, and
    `save_persona` raised `ValueError(f"Config file does not exist: {config_path}")`.
    So the structural test built to catch the leak could not see it twice over: it
    looked only at handlers catching OSError, and only at f-string interpolation,
    while this was a `str(exc)` on a ValueError.

    Reachable by racing the handler's own `is_file()` check, which is narrow -- and
    the message never needed the path, since every caller of `save_persona` already
    knows it.
    """

    def test_the_service_does_not_name_the_file(self, tmp_path) -> None:
        from coordinare.services.persona_service import save_persona

        missing = tmp_path / "deployment-secret-name.yaml"

        with pytest.raises(ValueError) as caught:
            save_persona("implementer", "Be brief.", missing)

        message = str(caught.value)
        assert "deployment-secret-name" not in message, f"path leaked: {message!r}"
        assert str(tmp_path) not in message, f"path leaked: {message!r}"

    @pytest.mark.parametrize(
        ("method", "url", "payload"),
        (
            ("put", "/api/personas/implementer", {"instructions": "Be brief."}),
            ("delete", "/api/personas/implementer", {}),
        ),
    )
    def test_losing_the_race_does_not_leak_it_either(
        self, temp_config_path, monkeypatch, method, url, payload,
    ) -> None:
        """The file vanishing between the handler's is_file() and the write.

        Reproduced by deleting it inside the service call, which is the only way to
        hit a window this narrow deterministically.
        """
        client = _client(temp_config_path)
        version = compute_content_hash(temp_config_path)

        from coordinare.services import persona_service

        real_save = persona_service.save_persona

        def vanish(*args, **kwargs):
            Path(temp_config_path).unlink(missing_ok=True)
            return real_save(*args, **kwargs)

        monkeypatch.setattr(persona_service, "save_persona", vanish)
        monkeypatch.setattr("coordinare.dashboard.save_persona", vanish, raising=False)

        resp = TestTheFiveRoutesRefuseAndPreserve._send(
            client, method, url, payload, headers={"If-Match": version},
        )

        body = resp.text
        assert str(temp_config_path) not in body, f"path leaked in {resp.status_code}: {body}"
        assert temp_config_path.name not in body, f"filename leaked in {resp.status_code}: {body}"


class TestSafeFailureReasonWithoutChmod:
    """Coverage that does not depend on chmod working.

    TestSafeFailureReason's OSError case skips when the user can read a 000 file,
    which is every root container. A fifth review round raised that and its own verify
    stage refuted it as hypothetical; the cheaper answer is to make the coverage
    unconditional rather than argue about who CI runs as.
    """

    def test_a_constructed_oserror_gives_strerror_only(self) -> None:
        from coordinare.services.config_write_service import safe_failure_reason

        exc = PermissionError(13, "Permission denied", "/etc/coordinare/config.yaml")

        reason = safe_failure_reason(exc)

        assert reason == "Permission denied"
        assert "/etc/coordinare" not in reason
        assert "config.yaml" not in reason

    def test_an_oserror_with_no_strerror_still_says_something(self) -> None:
        from coordinare.services.config_write_service import safe_failure_reason

        reason = safe_failure_reason(OSError("/etc/coordinare/config.yaml went away"))

        assert isinstance(reason, str) and reason
        assert "/etc/coordinare" not in reason, (
            "a single-argument OSError puts its whole message in args[0] and strerror "
            f"is None, so a fallback that used str(exc) would leak: {reason!r}"
        )

    def test_a_non_string_strerror_does_not_leak(self) -> None:
        from coordinare.services.config_write_service import safe_failure_reason

        exc = OSError()
        exc.strerror = Path("/etc/coordinare/config.yaml")  # type: ignore[assignment]

        reason = safe_failure_reason(exc)

        assert "/etc/coordinare" not in reason, (
            f"a non-str strerror was interpolated: {reason!r}"
        )


class TestThe204CarriesItsVersion:
    """The persona reset returns 204, and its refresh depends on a header on a body-less
    response.

    Verified rather than assumed: a 204 with a *body* is illegal and a 204 with headers
    is fine, but which of those Starlette does was not something to take on trust when
    the whole refresh path hangs on it. If the header were dropped, the reset would
    succeed and the next save in that session would 409 with nothing to explain it.
    """

    def test_the_reset_hands_back_a_version_that_works(self, temp_config_path) -> None:
        client = _client(temp_config_path)
        version = client.get("/api/personas").headers["ETag"]

        reset = client.delete(
            "/api/personas/implementer", headers={"If-Match": version},
        )

        assert reset.status_code == 204
        assert not reset.content, "a 204 must not carry a body"
        etag = reset.headers.get("ETag")
        assert etag, "the 204 dropped its ETag; the reset path cannot refresh its hash"
        assert etag.strip('"') == compute_content_hash(temp_config_path)

        following = client.put(
            "/api/personas/implementer",
            json={"instructions": "After a reset."},
            headers={"If-Match": etag},
        )

        assert following.status_code == 200, (
            f"the save after a reset got {following.status_code}; the version the 204 "
            f"handed back is not one the next write accepts"
        )


class TestAMalformedConfigIsARefusalNotACrash:
    """FR-020 — the persona writes catch what their writer actually raises.

    A sixth review round: ``save_persona`` and ``reset_persona`` both begin with
    ``yaml.safe_load(config_path.read_text())``, so a config file that is not valid
    YAML or not valid UTF-8 raises :class:`yaml.YAMLError` or
    :class:`UnicodeDecodeError` out of them. The handlers caught ``ValueError`` and
    ``OSError`` only, and neither is a superclass of either, so the exception left
    the handler and became a 500 with a traceback.

    The symphony write does not have this hole -- it wraps its persist in
    ``except Exception``. The asymmetry is the whole finding: the same malformed file
    is a structured refusal on one route and an unhandled crash on the other.
    """

    @staticmethod
    def _corrupt(config_path: Path, payload: bytes) -> str:
        """Break the file, then take the version *of the broken file*.

        Taken before the corruption it would be stale, and the guard would refuse the
        write with a 409 -- correctly, and the test would never reach the writer.
        """
        config_path.write_bytes(payload)
        return compute_content_hash(config_path)

    @pytest.mark.parametrize(
        ("label", "payload", "expected_reason"),
        (
            ("not YAML", b"personas: [unclosed\n", "the file is not valid YAML"),
            ("not UTF-8", b"personas:\n  implementer:\n    instructions: \xff\xfe\n", "the file is not valid UTF-8"),
        ),
    )
    @pytest.mark.parametrize("method", ("put", "delete"))
    def test_it_answers_rather_than_raising(
        self, temp_config_path, label, payload, expected_reason, method,
    ) -> None:
        client = _client(temp_config_path)
        version = self._corrupt(temp_config_path, payload)

        kwargs = {"headers": {"If-Match": version}}
        if method == "put":
            kwargs["json"] = {"instructions": "Be brief."}
        response = getattr(client, method)("/api/personas/implementer", **kwargs)

        assert response.status_code == 500, (
            f"a config that is {label} gave {response.status_code}; the handler is "
            f"expected to refuse, not to crash"
        )
        assert expected_reason in response.json()["error"]

    @pytest.mark.parametrize("method", ("put", "delete"))
    def test_the_refusal_does_not_name_the_file(self, temp_config_path, method) -> None:
        """SC-010 — the same leak rule as every other failure path here."""
        client = _client(temp_config_path)
        version = self._corrupt(temp_config_path, b"personas: [unclosed\n")

        kwargs = {"headers": {"If-Match": version}}
        if method == "put":
            kwargs["json"] = {"instructions": "Be brief."}
        response = getattr(client, method)("/api/personas/implementer", **kwargs)

        body = response.text
        assert str(temp_config_path) not in body, f"path leaked: {body!r}"
        assert temp_config_path.name not in body, f"path leaked: {body!r}"


class TestTheIfMatchHeaderIsParsedAsAHeaderNotAString:
    """FR-021 — RFC 7232 §3.2 shapes an ``If-Match`` can arrive in.

    The parse was ``removeprefix("W/").strip('"')``, which is right for the one shape
    the dashboard's own JS sends and wrong for every other legal one. The harm is not
    that a list fails -- it is that it fails as a 409 saying *"config changed since
    this page loaded"*, which is false. This spec has already shipped one error
    message that said something untrue about how to satisfy it.
    """

    @staticmethod
    def _version(client) -> str:
        return client.get("/api/personas").headers["ETag"]

    def test_a_list_matches_if_any_tag_matches(self, temp_config_path) -> None:
        client = _client(temp_config_path)
        current = self._version(client)

        response = client.put(
            "/api/personas/implementer",
            json={"instructions": "Be brief."},
            headers={"If-Match": f'"sha256:0000", {current}'},
        )

        assert response.status_code == 200, (
            f"a legal comma-separated If-Match got {response.status_code}; one of "
            f"those tags is the current version"
        )

    def test_whitespace_around_a_tag_does_not_break_it(self, temp_config_path) -> None:
        client = _client(temp_config_path)
        current = self._version(client)

        response = client.put(
            "/api/personas/implementer",
            json={"instructions": "Be brief."},
            headers={"If-Match": f"  {current} "},
        )

        assert response.status_code == 200

    def test_a_wildcard_is_refused_and_says_why(self, temp_config_path) -> None:
        """``*`` means "any representation", which is the guard opted out of.

        RFC 7232 defines it, and honouring it here would hand every caller a way to
        write without holding a version -- exactly what this spec removed. So it is
        refused; the point of the test is that it is refused *as a wildcard*, rather
        than falling through to "config changed since this page loaded", which would
        send an operator looking for a concurrent edit that never happened.
        """
        client = _client(temp_config_path)
        before = temp_config_path.read_bytes()

        response = client.put(
            "/api/personas/implementer",
            json={"instructions": "Be brief."},
            headers={"If-Match": "*"},
        )

        assert response.status_code == 428
        assert "*" in response.json()["error"]
        assert temp_config_path.read_bytes() == before, "the wildcard wrote anyway"

    def test_a_stale_tag_in_a_list_is_still_a_conflict(self, temp_config_path) -> None:
        """Any-match must not become always-match."""
        client = _client(temp_config_path)

        response = client.put(
            "/api/personas/implementer",
            json={"instructions": "Be brief."},
            headers={"If-Match": '"sha256:0000", "sha256:1111"'},
        )

        assert response.status_code == 409

    @pytest.mark.parametrize("header", (",,", '""', "  ", "W/"))
    def test_a_header_that_names_no_version_is_not_a_pass(
        self, temp_config_path, header,
    ) -> None:
        """Any-match must not degrade to no-match-required on an empty list.

        Parsing the header into a list of tags introduced this: iterate an empty list
        and every tag matched, vacuously. A header naming no version is the situation
        a missing header is in, and gets the same 428.
        """
        client = _client(temp_config_path)
        before = temp_config_path.read_bytes()

        response = client.put(
            "/api/personas/implementer",
            json={"instructions": "Be brief."},
            headers={"If-Match": header},
        )

        assert response.status_code == 428, (
            f"If-Match: {header!r} got {response.status_code}; it names no version"
        )
        assert temp_config_path.read_bytes() == before


class TestTheTagScannerRespectsQuotes:
    """FR-022 — a seventh review round, on the parser a sixth one wrote.

    ``_entity_tags`` split on every comma and then removed quotes, which is the
    obvious order and the wrong one: RFC 7232 2.3 lets an entity-tag contain a comma
    (``etagc`` is %x21 / %x23-7E), so a quoted tag was being torn into two.

    Nothing this deployment issues contains a comma, so no version in circulation
    could hit it, and the failure was to refuse rather than to permit. It is fixed
    anyway. A version parser answering the wrong question about a legal input is a
    defect regardless of who is currently asking, and this file has twice recorded
    "nothing reaches it yet" being wrong.
    """

    @staticmethod
    def _tags(header: str) -> list[str]:
        from coordinare.dashboard import _entity_tags

        return _entity_tags(header)

    def test_a_comma_inside_a_tag_does_not_split_it(self) -> None:
        assert self._tags('"abc,def"') == ["abc,def"]

    def test_a_comma_inside_one_of_several_does_not_either(self) -> None:
        assert self._tags('"tag1", "tag2,tag3"') == ["tag1", "tag2,tag3"]

    def test_a_separating_comma_still_separates(self) -> None:
        assert self._tags('"sha256:aa", "sha256:bb"') == ["sha256:aa", "sha256:bb"]

    def test_a_weak_tag_survives_the_space_it_should_not_have(self) -> None:
        """``W/ "x"`` is malformed -- RFC 7232 puts no space after ``W/``.

        It was leaving a stray quote on the front of the hash, so the recovery was
        wrong rather than absent. A malformed version should fail to match; it should
        not be mangled into a different string that then fails to match.
        """
        assert self._tags('W/ "hash123"') == ["hash123"]

    @pytest.mark.parametrize(
        ("header", "expected"),
        (
            ('"h"', ["h"]),
            ("h", ["h"]),  # a caller holding the bare hash, which the body vehicle hands out
            ('W/"h"', ["h"]),
            ('  "h"  ', ["h"]),
            ('"h",', ["h"]),
            ('"h" , "i"', ["h", "i"]),
            ("*", ["*"]),
            ('*, "h"', ["*", "h"]),  # the wildcard is still seen, and still refused
            ('""', []),
            (",,", []),
        ),
    )
    def test_the_shapes_that_arrive(self, header, expected) -> None:
        assert self._tags(header) == expected

    def test_an_unterminated_quote_does_not_swallow_a_separator_forever(self) -> None:
        """Quote-aware splitting means a stray quote changes what follows it.

        Recorded rather than defended: the tag comes out malformed, matches nothing
        and the write is refused, which is the right direction for a version that
        arrived broken.
        """
        assert self._tags('"h, "i"') == ['h, "i']


class TestTheDerivationsNoticeTheSourceChanging:
    """FR-023 — the machinery that catches stale tests was itself caching staleness.

    ``_reaching_a_writer`` and ``_guarded_write_handlers`` were
    ``@lru_cache(maxsize=1)`` over no arguments: a cache that cannot notice its input
    changing, guarding tests whose whole purpose is to notice their input changing.

    It matters because of how this spec is verified. Every structural fix since round
    four was checked by editing ``dashboard.py`` and re-running, and a derivation
    answering from before the edit would make a mutation look caught when it was not,
    or missed when it was not. Keying the cache on the source removes the question.
    """

    def test_a_changed_source_gives_a_changed_answer(self) -> None:
        source = _dashboard_source()
        before = _reaching_a_writer()

        mutated = source.replace(
            "def _version_headers(", "def _brand_new_writer_helper(x):\n    save_section(x)\n\n\ndef _version_headers(", 1,
        )
        after = _reaching_a_writer_for(mutated)

        assert "_brand_new_writer_helper" not in before
        assert "_brand_new_writer_helper" in after, (
            "the derivation answered from a cache keyed on nothing; a mutation to "
            "dashboard.py would be invisible to it"
        )

    def test_the_same_source_is_still_only_computed_once(self) -> None:
        """The cache is keyed, not removed -- these walks are the file's slowest part."""
        _reaching_a_writer()
        hits_before = _reaching_a_writer_for.cache_info().hits

        _reaching_a_writer()

        assert _reaching_a_writer_for.cache_info().hits > hits_before


class TestTheScannerAgainstTheGrammarItself:
    """SC-011 — an eighth review round, which found nothing, and left this behind.

    Every earlier test of the parser is an example someone thought of, which is the
    failure mode this file has recorded four times. These two are properties, checked
    over a generated corpus against a reference parser written from RFC 7232 2.3
    rather than from the implementation.

    Seeded, so a failure is reproducible and CI cannot flake.
    """

    #: entity-tag = [ weak ] DQUOTE *etagc DQUOTE, etagc = %x21 / %x23-7E / obs-text.
    #: A comma is in that range, which is what the seventh round's defect was about.
    _ETAGC = tuple(chr(c) for c in range(0x21, 0x7F) if c != 0x22)

    @staticmethod
    def _reference(header: str) -> list[str]:
        """A second parser, written from the grammar and not from _entity_tags.

        Two implementations of the same rule disagree where the rule was misread.
        That is the whole value of it, so this deliberately does not share code.
        """
        text = header.strip()
        if text == "*":
            return ["*"]
        out: list[str] = []
        index, size = 0, len(text)
        while index < size:
            while index < size and text[index] in " \t,":
                index += 1
            if index >= size:
                break
            if text.startswith("W/", index):
                index += 2
                while index < size and text[index] in " \t":
                    index += 1
            if index < size and text[index] == '"':
                close = text.find('"', index + 1)
                if close == -1:
                    out.append(text[index + 1 :].strip())
                    break
                out.append(text[index + 1 : close])
                index = close + 1
                while index < size and text[index] in " \t":
                    index += 1
                if index < size and text[index] == ",":
                    index += 1
            else:
                end = index
                while end < size and text[end] != ",":
                    end += 1
                token = text[index:end].strip()
                if token:
                    out.append(token)
                index = end + 1
        return [tag for tag in out if tag]

    def test_the_two_parsers_agree_on_every_legal_header(self) -> None:
        import random

        from coordinare.dashboard import _entity_tags

        rng = random.Random(3)
        disagreements = []
        for _ in range(1500):
            tags = [
                ("W/" if rng.random() < 0.3 else "")
                + '"'
                + "".join(rng.choice(self._ETAGC) for _ in range(rng.randint(0, 12)))
                + '"'
                for _ in range(rng.randint(1, 4))
            ]
            header = rng.choice([",", ", ", " ,", " , "]).join(tags)
            if _entity_tags(header) != self._reference(header):
                disagreements.append(header)

        assert not disagreements, (
            f"the scanner and the grammar disagree on legal input: {disagreements[:5]}"
        )

    def test_a_legal_list_round_trips_to_exactly_the_tags_it_named(self) -> None:
        import random

        from coordinare.dashboard import _entity_tags

        rng = random.Random(17)
        for _ in range(500):
            bodies = [
                "".join(rng.choice(self._ETAGC) for _ in range(rng.randint(1, 20)))
                for _ in range(rng.randint(1, 3))
            ]
            header = ", ".join(f'"{body}"' for body in bodies)

            assert _entity_tags(header) == bodies, f"{header!r} did not round-trip"

    def test_no_header_can_conjure_a_version_it_does_not_contain(self) -> None:
        """The property the whole branch rests on, stated once.

        Every example test asks whether a particular bad header is refused. This asks
        the general question: can any input at all parse to the current version
        without containing it? If it cannot, the guard cannot be talked past, whatever
        shape the header arrives in.
        """
        import random

        from coordinare.dashboard import _entity_tags

        truth = "sha256:" + "ab" * 32
        rng = random.Random(29)
        corpus = [
            "*", ",,", '""', "W/", 'W/ ""', '"a","b"', truth[:-1], truth.upper(),
            f'"{truth[:20]}"', '"a,b"', "null", "0", '"' + "x" * 500 + '"', "\x00",
            "sha256:", f'{truth[:-2]}"', "W/W/" + f'"{truth}x"',
        ]
        corpus += [
            "".join(rng.choice('"W/, sha256:abcdef*') for _ in range(rng.randint(1, 40)))
            for _ in range(1500)
        ]

        conjured = [h for h in corpus if truth not in h and truth in _entity_tags(h)]

        assert not conjured, (
            f"these headers parse to a version they do not contain: {conjured[:5]}"
        )
