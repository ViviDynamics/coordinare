"""Spec 144 / issue #198 — threat model, trust boundaries, and cheap hardening.

Covers the four stories:

  US1  a self-hoster can see what they are trusting (threat model, token matrix)
  US2  a hostile web page cannot drive the operator's dashboard (the guard)
  US3  an operator who exposes coordinare is told plainly (warning, bind config)
  US4  an operator can grant the least privilege that works (token matrix)

The guard's complete behaviour is specified in
``specs/144-threat-model-trust-boundaries/contracts/localhost-guard.md``. The
decision-table tests below mirror that contract row for row. If the two ever
disagree, fix the contract first and then the code.

Note the guard's two checks defend two *different* attacks, and neither
substitutes for the other:

  * ``Origin`` on mutating methods stops cross-site request forgery. ``Host`` is
    legitimately ``localhost`` in that attack, so only ``Origin`` reveals that
    the *page* is foreign.
  * ``Host`` on every method, reads included, stops DNS rebinding. A browser
    sends no ``Origin`` on a same-origin GET, so the origin check is structurally
    blind to it and reads would leak.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from coordinare.dashboard import DashboardStore, create_dashboard_app
from coordinare.localhost_guard import PermittedOrigins, build_permitted

REPO_ROOT = Path(__file__).resolve().parents[2]

DASHBOARD_PORT = 8090
LOCAL_BASE_URL = f"http://127.0.0.1:{DASHBOARD_PORT}"

MUTATING_METHODS = ("POST", "PUT", "PATCH", "DELETE")
SAFE_METHODS = ("GET", "HEAD", "OPTIONS")


def _permitted(trusted: list[str] | None = None) -> PermittedOrigins:
    return build_permitted(
        dashboard_host="127.0.0.1",
        dashboard_port=DASHBOARD_PORT,
        trusted_hosts=trusted or [],
    )


def _make_mock_daemon() -> MagicMock:
    daemon = MagicMock()
    daemon.state = {"phase": "idle", "error_count": 0}
    daemon.state_store = MagicMock()
    daemon.state_store.last_snapshot = None
    daemon._cycle_active = False
    daemon.running = True
    return daemon


def _make_mock_metrics() -> MagicMock:
    metrics = MagicMock()
    metrics.cycles_completed_total._value.get.return_value = 0
    metrics.build_info.labels.return_value._value.get.return_value = {
        "started_at": "2026-08-27T00:00:00+00:00"
    }
    return metrics


def _make_app() -> Any:
    return create_dashboard_app(
        DashboardStore(),
        _make_mock_daemon(),
        _make_mock_metrics(),
        MagicMock(),
    )


@pytest.fixture
def client() -> TestClient:
    """A client presenting a permitted local host, as a real browser would."""
    return TestClient(_make_app(), base_url=LOCAL_BASE_URL)


# ---------------------------------------------------------------------------
# US2 — permitted-set derivation (contract "Permitted sets", research D2)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "host",
    [
        "127.0.0.1",
        "127.0.0.1:8090",
        # In-range loopback that a hand-written list of spellings would reject.
        # This is the reason the implementation classifies with
        # ``ipaddress.is_loopback`` rather than enumerating known strings.
        "127.0.0.2",
        "127.0.0.2:8090",
        "localhost",
        "localhost:8090",
        "LOCALHOST",  # hostnames are case-insensitive
        "::1",  # bare IPv6, must survive port-stripping uncorrupted
        "[::1]",
        "[::1]:8090",
    ],
)
def test_permitted_hosts_accepts_every_loopback_spelling(host: str) -> None:
    """FR-014 — all spellings of loopback are legitimate local use."""
    assert _permitted().is_host_allowed(host), f"{host!r} should be permitted"


@pytest.mark.parametrize(
    "host",
    [
        "evil.example",
        "evil.example:8090",
        "example.com",
        "127.0.0.1.evil.example",  # suffix trick
        "notlocalhost",
        "0.0.0.0",  # not loopback; binding to it is not the same as being it
        "8.8.8.8",
        "testserver",  # the test-client default, deliberately NOT permitted
    ],
)
def test_permitted_hosts_rejects_foreign_hosts(host: str) -> None:
    """FR-011 — anything not local is refused, including the test default.

    ``testserver`` is listed explicitly: permitting it would be a permanent
    production hole, since the header is attacker-controlled.
    """
    assert not _permitted().is_host_allowed(host), f"{host!r} should be refused"


def test_port_is_not_used_to_accept_or_reject() -> None:
    """FR-013 as revised — the port is deliberately not compared.

    It was compared, and that was wrong twice over.

    It bought no security: a Host port is set by the browser from the URL it
    connected to, and a request cannot arrive on a port the server is not
    listening on, so a mismatched port can only come from a non-browser client,
    which this guard never authenticated. DNS rebinding is caught by the
    hostname check.

    And it broke real deployments. The end-to-end browser suite binds a random
    free port (``tests/e2e/conftest.py::_free_port``), so the browser sent
    ``Host: 127.0.0.1:<random>``, the guard answered 403 to every page load, and
    the CI job hung for 35 minutes before it was cancelled.
    """
    permitted = _permitted()
    for host in ("127.0.0.1:8090", "127.0.0.1:9999", "127.0.0.1:54321", "127.0.0.1"):
        assert permitted.is_host_allowed(host), f"{host!r} is loopback and must be permitted"

    # The property that actually matters is untouched.
    for host in ("evil.example:8090", "evil.example", "192.168.1.50:8090"):
        assert not permitted.is_host_allowed(host)


def test_permitted_hosts_honours_the_operator_allowlist() -> None:
    """FR-015 — an explicit entry widens the guard, nothing else does."""
    assert not _permitted().is_host_allowed("coordinare.internal")
    assert _permitted(["coordinare.internal"]).is_host_allowed("coordinare.internal")
    # Still scoped: adding one host does not admit another.
    assert not _permitted(["coordinare.internal"]).is_host_allowed("other.internal")


def test_bare_ipv6_is_not_corrupted_by_port_stripping() -> None:
    """Research D2, parsing rule 2 — ``rsplit(':', 1)`` would mangle ``::1``.

    A naive port-strip turns ``::1`` into ``:`` and the address stops parsing,
    so the guard would refuse a legitimate IPv6 loopback caller.
    """
    permitted = _permitted()
    assert permitted.is_host_allowed("::1")
    assert permitted.is_host_allowed("[::1]:8090")
    # Any port, since the port is no longer part of the decision.
    assert permitted.is_host_allowed("[::1]:54321")
    # The bracket handling itself is what this guards: a mangled address would
    # stop parsing as loopback and be refused.
    assert not permitted.is_host_allowed("[::1")


@pytest.mark.parametrize(
    "origin",
    [
        f"http://127.0.0.1:{DASHBOARD_PORT}",
        f"http://localhost:{DASHBOARD_PORT}",
        f"https://127.0.0.1:{DASHBOARD_PORT}",
        f"http://[::1]:{DASHBOARD_PORT}",
    ],
)
def test_permitted_origins_accepts_local_origins(origin: str) -> None:
    assert _permitted().is_origin_allowed(origin)


@pytest.mark.parametrize(
    "origin",
    [
        "https://evil.example",
        f"http://evil.example:{DASHBOARD_PORT}",
        "null",  # sandboxed iframe / some redirects
        "file://",
    ],
)
def test_permitted_origins_rejects_foreign_origins(origin: str) -> None:
    assert not _permitted().is_origin_allowed(origin)


def test_a_local_origin_on_another_port_is_permitted() -> None:
    """The deliberate consequence of dropping the port comparison.

    A page served from another port on the same machine counts as local. That
    follows the guard's own stated model, which the threat model spells out:
    this is a locality check, not authentication, and anyone who can make
    requests from your machine already has full control. Treating localhost:9999
    as hostile while localhost:8090 is trusted would be drawing a line the rest
    of the design does not draw.
    """
    assert _permitted().is_origin_allowed("http://127.0.0.1:9999")
    assert not _permitted().is_origin_allowed("http://evil.example:9999")


# ---------------------------------------------------------------------------
# US2 — the decision table, row by row (contract "Decision table")
# ---------------------------------------------------------------------------


def test_row1_absent_host_is_rejected() -> None:
    """Row 1 — HTTP/1.1 requires Host; its absence is malformed or hostile."""
    verdict = _permitted().check("GET", host=None, origin=None)
    assert not verdict.allowed
    assert verdict.rejected_header == "Host"


def test_row2_foreign_host_is_rejected_on_a_mutating_request() -> None:
    """Row 2 — FR-011."""
    verdict = _permitted().check("POST", host="evil.example", origin=None)
    assert not verdict.allowed
    assert verdict.rejected_header == "Host"


def test_row2_foreign_host_is_rejected_on_a_read_request() -> None:
    """Row 2 applied to GET — the DNS-rebinding case (FR-011).

    This is the row most likely to be "simplified" away by someone reasoning
    that reads are harmless. They are not: under DNS rebinding the browser
    believes the attacker's page shares an origin with the dashboard, sends no
    ``Origin`` on the same-origin GET, and can read the response. Before this
    guard, ``GET /api/config/global`` was exfiltratable that way.
    """
    verdict = _permitted().check("GET", host="evil.example", origin=None)
    assert not verdict.allowed
    assert verdict.rejected_header == "Host"


@pytest.mark.parametrize("method", SAFE_METHODS)
def test_row3_safe_method_with_good_host_is_allowed(method: str) -> None:
    """Row 3 — reads need no origin check once the host is known local."""
    assert _permitted().check(method, host="127.0.0.1:8090", origin=None).allowed


@pytest.mark.parametrize("method", MUTATING_METHODS)
def test_row4_absent_origin_is_allowed_on_mutating_requests(method: str) -> None:
    """Row 4 — FR-010. The row that looks wrong and is not.

    Browsers always send ``Origin`` on non-GET requests, so its absence means
    the caller is not a browser, and cross-site request forgery requires a
    browser. Rejecting these would break curl, scripts, and monitoring probes
    for no security gain. Row 2 still guards them.
    """
    assert _permitted().check(method, host="127.0.0.1:8090", origin=None).allowed


@pytest.mark.parametrize("method", MUTATING_METHODS)
def test_row5_foreign_origin_is_rejected_on_mutating_requests(method: str) -> None:
    """Row 5 — FR-009, the cross-site request forgery case."""
    verdict = _permitted().check(method, host="127.0.0.1:8090", origin="https://evil.example")
    assert not verdict.allowed
    assert verdict.rejected_header == "Origin"


@pytest.mark.parametrize("method", MUTATING_METHODS)
def test_row6_local_origin_and_host_is_allowed(method: str) -> None:
    """Row 6 — the dashboard's own requests must keep working."""
    verdict = _permitted().check(
        method, host="127.0.0.1:8090", origin=f"http://127.0.0.1:{DASHBOARD_PORT}"
    )
    assert verdict.allowed
    assert verdict.rejected_header is None


def test_a_foreign_origin_is_ignored_on_a_read() -> None:
    """Row 3 precedes rows 4-5: reads are not origin-checked.

    A cross-origin GET cannot forge a state change, and blocking it would break
    ordinary linking and embedding. The host check already covers rebinding.
    """
    assert _permitted().check("GET", host="127.0.0.1:8090", origin="https://evil.example").allowed


def test_every_rejection_explains_itself() -> None:
    """FR-016 — a rejection with no reason is the failure mode to avoid.

    An operator who put the dashboard behind a proxy and forgot
    ``trusted_dashboard_hosts`` should get a message telling them that, not an
    unexplained failure they debug for an hour.
    """
    for verdict in (
        _permitted().check("GET", host=None, origin=None),
        _permitted().check("GET", host="evil.example", origin=None),
        _permitted().check("POST", host="127.0.0.1:8090", origin="https://evil.example"),
    ):
        assert not verdict.allowed
        assert verdict.rejected_header in {"Host", "Origin"}
        assert verdict.reason, "a rejection must say why"


# ---------------------------------------------------------------------------
# US2 — the guard installed in the real application
# ---------------------------------------------------------------------------


def test_dashboard_page_still_loads(client: TestClient) -> None:
    """FR-017 — the guard must not break the dashboard's own surface."""
    assert client.get("/").status_code == 200


def test_sse_stream_is_permitted_by_the_guard() -> None:
    """FR-017 — /events must stay reachable for a local caller.

    Asserted at the decision level rather than by opening the stream: /events is
    an endless SSE response, so consuming it through the test client blocks until
    the suite times out. What matters is the guard's verdict, and a GET with a
    permitted Host takes row 3.
    """
    permitted = _permitted()
    assert permitted.check("GET", host="127.0.0.1:8090", origin=None).allowed
    # Still guarded against rebinding, like every other read.
    assert not permitted.check("GET", host="evil.example", origin=None).allowed


def test_mutating_route_rejects_a_foreign_origin_through_the_app(client: TestClient) -> None:
    """FR-009 end to end, not merely at the unit boundary."""
    response = client.post("/api/force-poll", headers={"Origin": "https://evil.example"})
    assert response.status_code == 403
    assert "origin" in response.text.lower()


def test_read_route_rejects_a_foreign_host_through_the_app(client: TestClient) -> None:
    """FR-011 end to end — the rebinding case, on a read."""
    response = client.get("/api/config/global", headers={"Host": "evil.example"})
    assert response.status_code == 403
    assert "host" in response.text.lower()


def test_rejection_does_not_echo_the_offending_value_unescaped(client: TestClient) -> None:
    """FR-016 — explain the failure without reflecting attacker content."""
    response = client.post(
        "/api/force-poll", headers={"Origin": "https://evil.example/<script>alert(1)</script>"}
    )
    assert response.status_code == 403
    assert "<script>" not in response.text


def test_guard_is_fail_closed_for_every_mutating_route(client: TestClient) -> None:
    """FR-012, SC-003 — the property that must survive future routes.

    Enumerates the application's **actual** route table rather than a fixed list
    of paths. A hardcoded list would go stale the moment someone adds a route,
    which is precisely the failure this test exists to prevent, so the test would
    stop protecting the property it names while continuing to pass.
    """
    app = client.app
    checked = 0
    for route in app.routes:
        methods = getattr(route, "methods", None) or set()
        mutating = sorted(methods & set(MUTATING_METHODS))
        if not mutating:
            continue
        path = route.path
        if "{" in path:
            # Fill path parameters with a placeholder; the guard runs before
            # routing resolves them, so the value is irrelevant.
            path = re.sub(r"\{[^}]+\}", "guard-probe", path)
        for method in mutating:
            response = client.request(method, path, headers={"Origin": "https://evil.example"})
            assert response.status_code == 403, (
                f"{method} {route.path} is not guarded against a foreign origin. "
                "The guard is middleware precisely so this cannot happen; if this "
                "fails, something bypassed it."
            )
            checked += 1

    assert checked >= 23, (
        f"expected to exercise at least the 23 mutating routes the app constructs, "
        f"checked {checked}. If this dropped, the route enumeration is no longer "
        "finding them and the fail-closed property is untested."
    )


def test_webhook_path_is_exempt_because_it_authenticates_itself() -> None:
    """The single deliberate exemption, and why it has to exist.

    ``register_webhook_route`` is called from ``__main__`` after the app is
    constructed, so the middleware would otherwise cover it. But GitHub calls
    that endpoint from the internet, and it authenticates itself with an HMAC
    signature over the body (401 on mismatch). Guarding it would break webhooks
    outright, which is a production outage rather than a hardening win.

    The exemption is an exact path match, never a prefix, so one entry cannot
    silently widen to a subtree.
    """
    import asyncio

    from coordinare.dashboard import register_webhook_route

    app = _make_app()
    trigger = asyncio.Event()
    register_webhook_route(app, "/webhook", "s3cret", trigger)

    guarded = TestClient(app, base_url=LOCAL_BASE_URL)
    # Without the exemption a foreign Host is refused, as for any other route.
    assert guarded.post("/webhook", headers={"Host": "github.example"}).status_code == 403

    exempt_app = create_dashboard_app(
        DashboardStore(),
        _make_mock_daemon(),
        _make_mock_metrics(),
        MagicMock(),
        guard_exempt_paths=frozenset({"/webhook"}),
    )
    register_webhook_route(exempt_app, "/webhook", "s3cret", asyncio.Event())
    exempt_client = TestClient(exempt_app, base_url=LOCAL_BASE_URL)

    # Reaches the handler, which then rejects on its own authentication (401),
    # not on the guard (403). That distinction is the whole point: the route is
    # exempt from the guard, not unauthenticated.
    response = exempt_client.post("/webhook", headers={"Host": "github.example"})
    assert response.status_code == 401, (
        "an exempt route must still authenticate itself; an exemption without "
        "authentication is simply an unguarded route"
    )

    # And the exemption does not widen to neighbouring paths.
    assert (
        exempt_client.post(
            "/api/force-poll", headers={"Origin": "https://evil.example"}
        ).status_code
        == 403
    )


# ---------------------------------------------------------------------------
# US3 — an operator who exposes coordinare is told plainly
# ---------------------------------------------------------------------------


def test_health_check_host_defaults_to_all_interfaces() -> None:
    """Research D4 — this default is a decision, and this test pins it.

    The tempting "consistency fix" is making health match ``dashboard_host``'s
    loopback default. That would silently break a documented deployment: the
    README presents the health endpoint as a load-balancer target, and a
    containerised daemon bound to loopback is unreachable from its host.

    The assertion exists so that change has to argue with a test rather than
    slip through as tidying. If you are here because this failed, read research
    D4 before changing the default.
    """
    from coordinare.config import ProjectConfiguration

    assert ProjectConfiguration.model_fields["health_check_host"].default == "0.0.0.0"
    assert ProjectConfiguration.model_fields["dashboard_host"].default == "127.0.0.1"


def test_trusted_dashboard_hosts_defaults_to_empty() -> None:
    """FR-015 — the safe posture is what you get by doing nothing."""
    from coordinare.config import ProjectConfiguration

    factory = ProjectConfiguration.model_fields["trusted_dashboard_hosts"].default_factory
    assert factory is not None
    assert factory() == []


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1"])
def test_loopback_bind_produces_no_warning(host: str) -> None:
    """FR-021 — a warning everyone sees on every start is one nobody reads."""
    from coordinare.localhost_guard import is_loopback_bind

    assert is_loopback_bind(host), f"{host!r} is loopback and must not warn"


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.10", "10.0.0.5", "::"])
def test_non_loopback_bind_is_flagged(host: str) -> None:
    """FR-020 — binding beyond loopback puts an unauthenticated control plane on a network."""
    from coordinare.localhost_guard import is_loopback_bind

    assert not is_loopback_bind(host), f"{host!r} is not loopback and must warn"


# ---------------------------------------------------------------------------
# US1 / US4 — the documents
# ---------------------------------------------------------------------------

THREAT_MODEL = "docs/security/threat-model.md"

#: The four boundaries FR-001 requires. Matched by heading, so rewording the
#: prose beneath one does not fail the test.
TRUST_BOUNDARY_HEADINGS = (
    "Operator host to daemon container",
    "Daemon to performer containers",
    "Performers to public GitHub content",
    "Coordinare to model endpoints",
)


def _read(rel: str) -> str:
    return (REPO_ROOT / rel).read_text(encoding="utf-8")


def _boundary_sections() -> dict[str, str]:
    """Each boundary heading mapped to the text under it."""
    text = _read(THREAT_MODEL)
    sections: dict[str, str] = {}
    for heading in TRUST_BOUNDARY_HEADINGS:
        match = re.search(
            rf"^#+\s*{re.escape(heading)}\s*$(.*?)(?=^#+\s|\Z)",
            text,
            re.MULTILINE | re.DOTALL | re.IGNORECASE,
        )
        if match:
            sections[heading] = match.group(1)
    return sections


def test_threat_model_covers_all_four_trust_boundaries() -> None:
    """FR-001."""
    found = _boundary_sections()
    missing = [h for h in TRUST_BOUNDARY_HEADINGS if h not in found]
    assert not missing, f"threat model is missing trust boundaries: {missing}"


def test_every_trust_boundary_states_a_residual_risk() -> None:
    """FR-003, data-model ``TrustBoundary`` validation rule.

    The load-bearing assertion of this whole document. A boundary described with
    mitigations and no residual risk is either wrong or is marketing, and a
    reader who later finds the gap themselves stops trusting everything else in
    the file.
    """
    for heading, body in _boundary_sections().items():
        assert "residual risk" in body.lower(), (
            f"trust boundary {heading!r} states no residual risk. If you believe there "
            "genuinely is none, say so explicitly and why, rather than omitting it."
        )


def test_threat_model_states_docker_sock_is_root_equivalent() -> None:
    """FR-002 — the single most consequential trust decision in the system."""
    text = _read(THREAT_MODEL).lower()
    assert "docker.sock" in text
    assert "root-equivalent" in text or "root equivalent" in text


def test_threat_model_has_an_honest_prompt_injection_section() -> None:
    """FR-003 — mitigations AND residual risk, not a list of defences."""
    text = _read(THREAT_MODEL)
    match = re.search(
        r"^#+\s*Prompt injection.*?(?=^#+\s|\Z)", text, re.MULTILINE | re.DOTALL | re.I
    )
    assert match, "threat model has no prompt-injection section"
    body = match.group(0).lower()

    assert "residual" in body, "the prompt-injection section must state residual risk"
    # It should name the mitigations that actually exist rather than gesturing.
    for mitigation in ("reviewer", "human", "scan"):
        assert mitigation in body, f"prompt-injection section should reference {mitigation!r}"


def test_threat_model_explains_the_unauthenticated_dashboard() -> None:
    """FR-004 — a deliberate decision with a boundary and a future path."""
    text = _read(THREAT_MODEL).lower()
    assert "unauthenticated" in text
    assert "loopback" in text
    assert "143" in text, "the dashboard section must name spec 143 as the auth path"


def test_threat_model_describes_health_endpoint_disclosure() -> None:
    """FR-005 — what the always-bound port gives away."""
    text = _read(THREAT_MODEL).lower()
    assert "/metrics" in text
    assert "health_check_host" in text, (
        "the threat model must name the config field, since keeping its 0.0.0.0 "
        "default is a recorded residual risk rather than an oversight"
    )


def test_threat_model_states_performer_output_is_untrusted() -> None:
    """FR-006."""
    text = _read(THREAT_MODEL).lower()
    assert "untrusted" in text
    assert "semgrep" in text or "security scan" in text or "scan gate" in text


def test_token_permission_matrix_is_documented() -> None:
    """FR-023, FR-024, FR-025."""
    text = _read(THREAT_MODEL)
    lowered = text.lower()

    assert "fine-grained" in lowered, "the matrix must specify fine-grained permissions"

    # Pin the two corrections found by auditing src/coordinare/services/github.py
    # on 2026-08-27. Both were wrong in the first draft, and both would have
    # surfaced as a mid-run failure instead of at setup, which is the exact
    # failure this matrix exists to prevent.
    assert "administration" in lowered, (
        "the matrix must list Administration: read, needed by GetBranchProtection "
        "(used from monitor_performer.py)"
    )
    issues_row = next((row for row in text.splitlines() if row.startswith("| Read issues")), "")
    assert "Read and write" in issues_row, (
        "Issues needs write, not read: coordinare posts comments via AddComment and "
        "manages labels via CreateLabel/AddLabels"
    )
    assert "minimal" in lowered, (
        "the matrix must distinguish a minimal first run from optional features (FR-024)"
    )
    assert "github app" in lowered, "note the GitHub App direction of travel (FR-025)"


def test_readme_links_the_threat_model_prominently() -> None:
    """FR-007 — encountered before deploying, not buried."""
    readme = _read("README.md")
    assert "docs/security/threat-model.md" in readme

    # "Prominent" is judged as: above the halfway point of the file.
    position = readme.index("docs/security/threat-model.md")
    assert position < len(readme) / 2, (
        "the threat-model link sits in the bottom half of the README. It is meant to "
        "be seen before deployment, not found afterwards."
    )


def test_security_md_reserved_section_is_filled() -> None:
    """FR-008 — spec 142 shaped this section to receive exactly this content."""
    text = _read("SECURITY.md")

    assert "Reserved for spec 144" not in text, (
        "SECURITY.md still carries spec 142's placeholder comment; the section was "
        "reserved to be filled, not left"
    )
    assert "threat-model.md" in text, "SECURITY.md must point at the threat model"
    # Spec 142's own test asserts this file names spec 144; keep that true.
    assert "144" in text


def test_every_test_cited_by_the_threat_model_exists() -> None:
    """Research D5 — the only automated check on the prose, deliberately.

    Asserting document *wording* is brittle: it fails when someone improves a
    sentence, which trains people to weaken the test. Asserting nothing lets the
    document drift into describing a system that no longer exists, which is worse
    than having no document because it is confidently wrong.

    Citing test names resolves it. The claim and its evidence sit together, a
    reader can run the named test, and an editor who wants to change a claim can
    see what will contradict them. All this check does is stop a rename leaving a
    dangling citation.
    """
    text = _read(THREAT_MODEL)
    cited = set(re.findall(r"`(test_[a-z0-9_]+)`", text))
    assert cited, (
        "the threat model cites no tests. Its checkable claims are supposed to name "
        "the tests that hold them up (FR-026)."
    )

    own_source = Path(__file__).read_text(encoding="utf-8")
    defined = set(re.findall(r"^def (test_[a-z0-9_]+)", own_source, re.MULTILINE))

    dangling = sorted(cited - defined)
    assert not dangling, (
        f"the threat model cites tests that do not exist: {dangling}. "
        "Either the test was renamed and the citation is stale, or the claim has no "
        "evidence behind it."
    )


def test_exposed_bind_actually_emits_the_warning() -> None:
    """FR-020 — exercise the warning path, not just the predicate behind it.

    This test exists because an earlier version of this code lived inline in
    ``__main__`` and referenced an undefined logger name. It would have raised
    ``NameError`` at exactly the moment it was supposed to warn. Lint caught it;
    no test did, because nothing executed that path.
    """
    from coordinare.localhost_guard import warn_if_dashboard_exposed

    captured: list[tuple[str, dict]] = []

    class _Recorder:
        def warning(self, event: str, **kw: object) -> None:
            captured.append((event, dict(kw)))

    warned = warn_if_dashboard_exposed("0.0.0.0", 8090, log=_Recorder())
    assert warned is True
    assert len(captured) == 1
    event, fields = captured[0]
    assert event == "dashboard_exposed_without_authentication"
    assert fields["dashboard_host"] == "0.0.0.0"
    detail = str(fields["detail"]).lower()
    assert "no authentication" in detail, "the warning must name the actual risk"


def test_loopback_bind_emits_nothing() -> None:
    """FR-021 — silence on the default, so the warning keeps its meaning."""
    from coordinare.localhost_guard import warn_if_dashboard_exposed

    captured: list[str] = []

    class _Recorder:
        def warning(self, event: str, **kw: object) -> None:
            captured.append(event)

    assert warn_if_dashboard_exposed("127.0.0.1", 8090, log=_Recorder()) is False
    assert captured == []


# ---------------------------------------------------------------------------
# Findings from the adversarial review round (2026-08-27)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "origin",
    [
        "http:// 127.0.0.1:8090",
        "http://  127.0.0.1:8090",
        "http://\t127.0.0.1:8090",
        "http://127.0.0.1 :8090",
        "http://\n127.0.0.1:8090",
    ],
)
def test_malformed_origin_with_internal_whitespace_is_rejected(origin: str) -> None:
    """Adversarial review finding 1 — whitespace must not be laundered.

    An earlier version stripped in two places: ``is_origin_allowed`` sliced the
    scheme off leaving a leading space, and ``_split_host_port`` stripped that
    away again, so ``http:// 127.0.0.1:8090`` parsed as permitted.

    Worth being clear about the severity, because the review called it critical
    and that overstates it. A browser builds the ``Origin`` header itself from
    the page's origin and will never insert whitespace, so a genuine forged
    request cannot reach this. Anyone able to set arbitrary headers is a direct
    client, not a forged browser request, and the guard was never authentication.
    It is fixed because strict parsing is correct and the contract requires it,
    not because it was an exploitable hole.
    """
    assert not _permitted().is_origin_allowed(origin)


@pytest.mark.parametrize("host", ["127.0.0.1 :8090", "127.0.0. 1", "local host"])
def test_malformed_host_with_internal_whitespace_is_rejected(host: str) -> None:
    """Same defect on the host side."""
    assert not _permitted().is_host_allowed(host)


def test_surrounding_whitespace_is_still_tolerated() -> None:
    """The fix must reject *internal* whitespace without breaking ordinary values.

    Header values can pick up surrounding whitespace legitimately; only embedded
    whitespace indicates a malformed value.
    """
    permitted = _permitted()
    assert permitted.is_host_allowed(" 127.0.0.1:8090 ")
    assert permitted.is_origin_allowed(" http://127.0.0.1:8090 ")


def test_exemptions_are_exact_matches_not_prefixes() -> None:
    """Adversarial review finding 2 — a genuine test gap.

    The review demonstrated a mutation (``in exempt`` becoming a ``startswith``
    check) that kept all 79 tests green while silently un-guarding every path
    under an exempt prefix. The contract forbids prefix matching, but nothing
    asserted it, so the contract was decorative on this point.

    A sibling path under an exempt one must stay guarded.
    """
    import asyncio

    from coordinare.dashboard import register_webhook_route

    app = create_dashboard_app(
        DashboardStore(),
        _make_mock_daemon(),
        _make_mock_metrics(),
        MagicMock(),
        guard_exempt_paths=frozenset({"/webhook"}),
    )
    register_webhook_route(app, "/webhook", "s3cret", asyncio.Event())

    @app.post("/webhook/sneaky")
    async def _sneaky() -> dict[str, str]:  # pragma: no cover - must never run
        return {"did": "mutate"}

    client = TestClient(app, base_url=LOCAL_BASE_URL)

    # The exempt path itself reaches its own authentication.
    assert client.post("/webhook", headers={"Host": "github.example"}).status_code == 401

    # A path merely *starting with* it must still be guarded.
    assert client.post("/webhook/sneaky", headers={"Host": "github.example"}).status_code == 403, (
        "exemptions must be exact matches. A prefix match would un-guard every path "
        "under an exempt one, which is how a single narrow exemption becomes a hole."
    )


def test_local_origin_is_allowed_through_every_mutating_route() -> None:
    """Adversarial review finding 3 — the fail-closed test only proved half of it.

    ``test_guard_is_fail_closed_for_every_mutating_route`` asserts foreign
    origins are rejected, so a guard that rejected *everything* would satisfy it
    while breaking the dashboard completely. This asserts the other half: a
    legitimate local caller is not blocked.

    The assertion is that *the guard* did not refuse, identified by its error body
    rather than by the bare status. A plain "not 403" check is wrong here: some
    handlers legitimately return 403 of their own, for instance
    ``PUT /api/config/section/{id}`` answering "Config file not available" when no
    config path is configured. Conflating the two would make this test fail for a
    reason that has nothing to do with the guard.
    """
    client = TestClient(_make_app(), base_url=LOCAL_BASE_URL)
    local_origin = f"http://127.0.0.1:{DASHBOARD_PORT}"

    checked = 0
    for route in client.app.routes:
        methods = getattr(route, "methods", None) or set()
        for method in sorted(methods & set(MUTATING_METHODS)):
            path = re.sub(r"\{[^}]+\}", "guard-probe", route.path)
            response = client.request(method, path, headers={"Origin": local_origin})
            if response.status_code == 403:
                assert "rejected_by_localhost_guard" not in response.text, (
                    f"{method} {route.path} was refused by the GUARD for a legitimate "
                    "local caller. The guard is meant to block foreign origins, not the "
                    "dashboard's own requests."
                )
            checked += 1

    assert checked >= 23


def test_exposed_bind_warning_explains_the_guard_consequence() -> None:
    """Found while checking what the adversarial review's empty lenses missed.

    An operator who sets ``dashboard_host`` to ``0.0.0.0`` to reach the dashboard
    from another machine browses to something like ``192.168.1.50:8090``. That
    Host is not loopback, is not ``localhost``, and is not the literal
    ``0.0.0.0``, so the guard refuses **every request** with 403.

    The guard is right to do that: an exposed dashboard must declare which
    hostnames it answers to. But a correct refusal nobody can diagnose is still
    an outage, and nothing else surfaces ``trusted_dashboard_hosts``. The warning
    that already fires for this exact case has to say so.
    """
    from coordinare.localhost_guard import build_permitted, warn_if_dashboard_exposed

    # The situation that motivates this: the address they would actually use.
    permitted = build_permitted("0.0.0.0", 8090, trusted_hosts=[])
    assert not permitted.is_host_allowed("192.168.1.50:8090")
    # ...and the escape hatch does work, once they know it exists.
    assert build_permitted("0.0.0.0", 8090, trusted_hosts=["192.168.1.50"]).is_host_allowed(
        "192.168.1.50:8090"
    )

    captured: list[dict] = []

    class _Recorder:
        def warning(self, event: str, **kw: object) -> None:
            captured.append(dict(kw))

    assert warn_if_dashboard_exposed("0.0.0.0", 8090, log=_Recorder()) is True
    action = str(captured[0].get("action_required", "")).lower()
    assert "trusted_dashboard_hosts" in action, (
        "the warning must name the setting, or the operator has no way to find it"
    )
    assert "403" in action, "it should say what the failure looks like, so it is recognisable"


class TestBindAllIsNeverAPermittedHost:
    """A bind address is not a hostname, and trusting it re-opens the hole.

    Found while reviewing spec 147: the Helm chart must set ``dashboard_host`` to
    ``0.0.0.0`` for an in-cluster Service to reach the dashboard at all, and
    ``build_permitted`` used to add whatever ``dashboard_host`` said to the
    permitted set. That admitted ``Host: 0.0.0.0``.

    Which is not academic. Browsers on macOS and Linux route
    ``http://0.0.0.0:<port>`` to loopback — the "0.0.0.0 day" quirk — so a
    malicious page open in the operator's browser could reach a dashboard they
    had port-forwarded, with a Host the guard accepted. That is exactly the
    cross-origin request this guard exists to refuse.
    """

    def test_a_bind_all_dashboard_host_is_not_trusted(self) -> None:
        from coordinare.localhost_guard import build_permitted

        permitted = build_permitted(dashboard_host="0.0.0.0", dashboard_port=8090)
        assert not permitted.is_host_allowed("0.0.0.0:8090")
        assert not permitted.is_host_allowed("0.0.0.0")
        assert not permitted.is_origin_allowed("http://0.0.0.0:8090")

    def test_the_ipv6_bind_all_is_not_trusted_either(self) -> None:
        from coordinare.localhost_guard import build_permitted

        permitted = build_permitted(dashboard_host="::", dashboard_port=8090)
        assert not permitted.is_host_allowed("[::]:8090")
        assert not permitted.is_host_allowed("::")

    @pytest.mark.parametrize(
        "spelling",
        [
            "::0",
            "0:0:0:0:0:0:0:0",
            "0000:0000:0000:0000:0000:0000:0000:0000",
            "::ffff:0.0.0.0",
            "0",
            "00.00.00.00",
            "0x0",
        ],
    )
    def test_every_spelling_of_a_bind_all_address_is_caught(self, spelling: str) -> None:
        """Not spellings anyone would choose, but ones a server really binds.

        The first version of this guard compared ``dashboard_host`` against a
        hand-written set of strings, and every value here slipped past it while
        still resolving to an unspecified address — so setting any of them would
        have restored the hole in full. Enumerating the spellings of a number is a
        losing game; the guard now asks what the value resolves to.
        """
        from coordinare.localhost_guard import build_permitted

        permitted = build_permitted(dashboard_host=spelling, dashboard_port=8090)
        assert not permitted.is_host_allowed(spelling), (
            f"{spelling!r} resolves to a bind-all address and must never be trusted"
        )

    def test_a_hostname_is_not_mistaken_for_a_bind_address(self) -> None:
        """Resolution must not reach the network, nor swallow real hostnames.

        A hostname is rejected by the numeric-only parse and left to the
        trusted-host list, which is where an operator's deliberate opt-in belongs.
        """
        from coordinare.localhost_guard import _is_wildcard_bind

        for name in ("coordinare.ns.svc.cluster.local", "localhost", "example.com"):
            assert not _is_wildcard_bind(name)

    def test_a_specific_bind_address_is_still_trusted(self) -> None:
        """Only the *unspecified* address is refused, not any non-loopback bind.

        An operator who binds to one real interface has named a reachable host,
        and that remains as trusted as it was before this change.
        """
        from coordinare.localhost_guard import build_permitted

        permitted = build_permitted(dashboard_host="192.168.1.5", dashboard_port=8090)
        assert permitted.is_host_allowed("192.168.1.5:8090")

    def test_binding_wide_does_not_stop_loopback_callers(self) -> None:
        """The operator's own access must survive the fix.

        Binding to every interface is exactly what a containerised deployment
        does, and `kubectl port-forward` reaches it as 127.0.0.1.
        """
        from coordinare.localhost_guard import build_permitted

        permitted = build_permitted(dashboard_host="0.0.0.0", dashboard_port=8090)
        assert permitted.is_host_allowed("127.0.0.1:8090")
        assert permitted.is_host_allowed("localhost:8090")

    def test_a_real_hostname_is_still_trusted_when_named(self) -> None:
        """The opt-in path is unaffected; only the bind-all shortcut is closed."""
        from coordinare.localhost_guard import build_permitted

        permitted = build_permitted(
            dashboard_host="0.0.0.0", dashboard_port=8090, trusted_hosts=["coordinare.ns.svc"]
        )
        assert permitted.is_host_allowed("coordinare.ns.svc:8090")
