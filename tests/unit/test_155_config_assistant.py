"""Spec 155 / issue #202 — the config assistant.

Two properties carry this feature, and neither is a thing the model is asked to
respect:

* it cannot write configuration, because there is no write path in the module;
* a secret value cannot reach the model, because the masked view the UI already
  renders is the only view it is given.

Both are asserted structurally. "We told it not to" is not a guarantee about a
system whose characteristic failure is not doing what it was told.

The third property is the one spec 124 paid for: exactly one model call per turn.
Tool-calling loops were unreliable across every self-hosted model available, and
this must work on a self-hosted endpoint, so a loop reintroduced later fails here.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import pytest

MODULE = Path("src/coordinare/services/config_assistant.py")

#: Anything that could put bytes on disk. A write reached through any of these
#: would defeat the whole propose-only design.
WRITE_SURFACE = {
    "atomic_write_yaml",
    "config_write_service",
    "write_text",
    "write_bytes",
    "safe_dump",
    "mkstemp",
    "replace",
    "rename",
}

SECRET_VALUE = "ghp_realsecrettokenvalue1234567890"


class ScriptedBackend:
    """A conducting backend that returns what the test tells it to, and records calls.

    Not ``AsyncMock``: this must be able to fail an assertion when it is called
    more times than the test allows, and a mock that invents attributes cannot
    distinguish "called twice" from "called once with a typo'd assertion".
    """

    def __init__(self, responses: list[Any] | None = None) -> None:
        self.responses = list(responses or [])
        self.prompts: list[str] = []

    async def prompt(self, text: str, response_format: str | None = None) -> Any:
        self.prompts.append(text)
        if not self.responses:
            raise AssertionError(
                f"the assistant called the model {len(self.prompts)} times; the test "
                "scripted fewer. One structured response per turn — a tool-calling "
                "loop is what spec 124 found unreliable on self-hosted models.",
            )
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class TestItCannotWriteConfiguration:
    """SC-001 — the safety property, asserted on the code rather than on behaviour."""

    def test_the_module_has_no_write_path(self) -> None:
        tree = ast.parse(MODULE.read_text())
        offenders: list[str] = []

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if any(w in alias.name for w in WRITE_SURFACE):
                        offenders.append(f"import {alias.name}:{node.lineno}")
            elif isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                if any(w in mod for w in WRITE_SURFACE):
                    offenders.append(f"from {mod}:{node.lineno}")
                for alias in node.names:
                    if alias.name in WRITE_SURFACE:
                        offenders.append(f"from {mod} import {alias.name}:{node.lineno}")
            elif isinstance(node, ast.Call):
                func = node.func
                name = getattr(func, "attr", None) or getattr(func, "id", None)
                if name in WRITE_SURFACE:
                    offenders.append(f"call {name}:{node.lineno}")
                if name == "open":
                    for arg in node.args[1:]:
                        if isinstance(arg, ast.Constant) and "w" in str(arg.value):
                            offenders.append(f"open(..., 'w'):{node.lineno}")

        assert not offenders, (
            "the config assistant gained a way to write configuration: "
            f"{offenders}. Applying is a human click through the dashboard's existing "
            "write path; an assistant that can write is a way to change coordinare's "
            "behaviour by talking to it."
        )


class TestSecretsNeverReachTheModel:
    """SC-002 — asserted on the prompts actually sent, not on the masking helper."""

    @staticmethod
    def _config_holding_a_secret():
        """A real config whose token is a real-looking secret.

        Built from raw rather than by assignment: ``CoordinareConfiguration`` does
        not expose ``github_token`` as a settable attribute, so mutating the
        fixture raises — and a test that skipped the secret entirely would pass
        while proving nothing.
        """
        import sys

        sys.path.insert(0, "tests/unit")
        from conftest import REPRESENTATIVE_CONFIG
        from coordinare.config import CoordinareConfiguration
        from coordinare.config_validation import coerce_multi_symphony_raw

        raw = {**REPRESENTATIVE_CONFIG, "github_token": SECRET_VALUE}
        return CoordinareConfiguration(**coerce_multi_symphony_raw(raw))

    @pytest.mark.asyncio
    async def test_a_secret_value_appears_in_no_prompt(self) -> None:
        from coordinare.services.config_assistant import run_turn

        coordinare_config = self._config_holding_a_secret()
        backend = ScriptedBackend([{"reply": "ok", "proposal": None}])
        await run_turn(
            message="what is my github token?",
            history=[],
            config=coordinare_config,
            backend=backend,
        )

        assert backend.prompts, "the test proves nothing if the model was never called"
        for prompt in backend.prompts:
            assert SECRET_VALUE not in prompt, (
                "a secret value reached the model. The assistant sends config to an "
                "endpoint the operator may not control; this is the config's most "
                "sensitive content going somewhere they did not choose."
            )


class TestMalformedOutputNeverBecomesAProposal:
    """SC-006 / FR-012 — a broken model must produce a plain failure, not a half-proposal."""

    @pytest.mark.parametrize(
        "bad",
        [
            pytest.param(None, id="backend-returned-nothing"),
            pytest.param("not a dict", id="not-an-object"),
            pytest.param({}, id="empty-object"),
            pytest.param({"proposal": {"section": "global"}}, id="proposal-without-values"),
            pytest.param({"proposal": {"values": {"a": 1}}}, id="proposal-without-section"),
            pytest.param({"reply": "hi", "proposal": "a string"}, id="proposal-not-an-object"),
            pytest.param({"reply": 42}, id="reply-not-a-string"),
        ],
    )
    @pytest.mark.asyncio
    async def test_it_yields_an_error_and_no_proposal(self, coordinare_config, bad) -> None:
        from coordinare.services.config_assistant import run_turn

        turn = await run_turn(
            message="set something",
            history=[],
            config=coordinare_config,
            backend=ScriptedBackend([bad]),
        )

        assert turn.proposal is None, f"{bad!r} produced a proposal an operator could apply"
        assert turn.error, "a failure must be reported, not swallowed into an empty reply"

    @pytest.mark.asyncio
    async def test_a_backend_that_raises_is_reported(self, coordinare_config) -> None:
        from coordinare.services.config_assistant import run_turn

        turn = await run_turn(
            message="hello",
            history=[],
            config=coordinare_config,
            backend=ScriptedBackend([RuntimeError("endpoint down")]),
        )

        assert turn.proposal is None
        assert turn.error


class TestAProposalMustNameSomethingReal:
    """FR-005 — rejected here, so nothing unreal ever reaches the apply path."""

    @pytest.mark.asyncio
    async def test_an_unknown_section_is_rejected(self, coordinare_config) -> None:
        from coordinare.services.config_assistant import run_turn

        turn = await run_turn(
            message="configure the flux capacitor",
            history=[],
            config=coordinare_config,
            backend=ScriptedBackend(
                [{"reply": "sure", "proposal": {"section": "flux", "values": {"jigawatts": 1.21}}}],
            ),
        )
        assert turn.proposal is None
        assert turn.error and "flux" in turn.error

    @pytest.mark.asyncio
    async def test_an_unknown_field_is_rejected(self, coordinare_config) -> None:
        from coordinare.services.config_assistant import run_turn

        turn = await run_turn(
            message="set a thing",
            history=[],
            config=coordinare_config,
            backend=ScriptedBackend(
                [
                    {
                        "reply": "sure",
                        "proposal": {"section": "global", "values": {"no_such_field": 1}},
                    },
                ],
            ),
        )
        assert turn.proposal is None
        assert turn.error and "no_such_field" in turn.error


class TestSecretsCannotBeSetByProposal:
    """FR-009. What this turned into is better than what was asked for.

    ``github_token`` is secret *and* is not in the set ``PUT /api/config/global``
    accepts, so the assistant cannot propose it at all — not even as an env-var
    reference. That is the right posture: a token belongs in the environment, not
    in ``config.yaml``, so the assistant's job there is to tell the operator to
    set the variable, not to write anything.

    The literal-secret rule is kept as depth, for the day a secret-bearing field
    becomes editable. It is exercised directly rather than pretended into being
    reachable.
    """

    def test_the_token_cannot_be_proposed_at_all(self, coordinare_config) -> None:
        from coordinare.services.config_assistant import Proposal, validate_proposal

        rejection = validate_proposal(
            coordinare_config,
            Proposal(section="global", values={"github_token": "${GITHUB_TOKEN}"}),
        )

        assert rejection is not None, (
            "the assistant offered to write the token into config.yaml; it belongs "
            "in the environment"
        )
        assert "github_token" in rejection

    def test_no_editable_global_field_is_secret_today(self, coordinare_config) -> None:
        """Pins the assumption above, so adding a secret editable field is noticed."""
        from coordinare.config_descriptors import GLOBAL_EDITABLE_FIELDS
        from coordinare.services.config_assistant import _secret_fields

        overlap = _secret_fields(coordinare_config, "global") & set(GLOBAL_EDITABLE_FIELDS)

        assert not overlap, (
            f"{sorted(overlap)} is now both secret and editable, so the literal-secret "
            "rule is live — make sure it is exercised for real, not just in the "
            "monkeypatched test below"
        )

    def test_a_literal_value_for_a_secret_field_is_rejected(
        self, coordinare_config, monkeypatch,
    ) -> None:
        """The rule itself, on a field made secret for the duration."""
        from coordinare.services import config_assistant as mod

        # A string-typed field, so the only thing that can reject the value is the
        # secret rule. Using an int field would have it rejected for its type
        # instead, and the test would pass without exercising the rule at all.
        monkeypatch.setattr(mod, "_secret_fields", lambda _c, _s: {"assignee_filter"})

        rejection = mod.validate_proposal(
            coordinare_config,
            mod.Proposal(section="global", values={"assignee_filter": "hunter2"}),
        )
        assert rejection is not None and "environment variable" in rejection

    def test_an_env_reference_for_a_secret_field_is_accepted(
        self, coordinare_config, monkeypatch,
    ) -> None:
        from coordinare.services import config_assistant as mod

        monkeypatch.setattr(mod, "_secret_fields", lambda _c, _s: {"assignee_filter"})

        rejection = mod.validate_proposal(
            coordinare_config,
            mod.Proposal(section="global", values={"assignee_filter": "${WHO}"}),
        )
        assert rejection is None, rejection

    def test_a_literal_cannot_ride_alongside_a_placeholder(
        self, coordinare_config, monkeypatch,
    ) -> None:
        """Review finding: `is_env_placeholder` asks whether a value *contains* one.

        That is the right question for display, and the wrong one here — it would
        accept "${TOKEN} ghp_realsecret", putting the literal into config.yaml
        beside the reference that was supposed to replace it.
        """
        from coordinare.services import config_assistant as mod

        monkeypatch.setattr(mod, "_secret_fields", lambda _c, _s: {"assignee_filter"})

        rejection = mod.validate_proposal(
            coordinare_config,
            mod.Proposal(
                section="global", values={"assignee_filter": "${WHO} ghp_realsecret"},
            ),
        )
        assert rejection is not None, "a literal secret rode in beside a placeholder"


class TestOneCallPerTurn:
    """FR-011 — the constraint spec 124 bought with a failed POC."""

    @pytest.mark.asyncio
    async def test_the_model_is_called_exactly_once(self, coordinare_config) -> None:
        from coordinare.services.config_assistant import run_turn

        backend = ScriptedBackend([{"reply": "hello", "proposal": None}])
        await run_turn(
            message="hi", history=[], config=coordinare_config, backend=backend,
        )

        assert len(backend.prompts) == 1, (
            "more than one model call in a turn is an agentic loop, which is the "
            "mechanism spec 124 found unreliable on every self-hosted model tried"
        )


class TestTheContextIsBoundedAndHonest:
    """FR-017 — silent truncation makes a model confidently wrong about what it never saw."""

    def test_an_oversized_config_is_bounded_and_says_so(self, coordinare_config) -> None:
        from coordinare.services.config_assistant import _CHARS_PER_TOKEN, build_context

        full = build_context(coordinare_config, budget=100_000)
        tight = build_context(coordinare_config, budget=100)

        assert len(tight) < len(full), "the budget did not bound anything"
        # The note naming what was omitted is added after the budget is spent, so
        # allow for it rather than pretending the limit is exact.
        assert len(tight) <= 100 * _CHARS_PER_TOKEN + 400, (
            f"the budget was not respected in any meaningful sense: {len(tight)} chars"
        )
        assert "omitted" in tight.lower() or "not shown" in tight.lower(), (
            "context was truncated without telling the model, so it will answer "
            "confidently about fields it was never given"
        )


class TestApplyingGoesThroughTheGuardedWritePath:
    """SC-003 / FR-006, FR-007.

    The issue for #202 said applying "uses the SHA-256 concurrency guard" that the
    config UI already has. It does — for catalogs and routing. ``PUT
    /api/config/global`` had no hash check at all, and that is the endpoint a
    config proposal most often targets (#237). These pin the guard that was added.
    """

    @staticmethod
    def _client(config_path):
        """A dashboard client over a config the endpoint will actually accept.

        The shared fixture writes ``${VAR}`` for the token, and this endpoint
        re-validates the whole file after merging — which rejects an unresolved
        placeholder. That is pre-existing behaviour and not what these tests are
        about, so the file gets a resolved token.
        """
        from unittest.mock import MagicMock

        import yaml
        from fastapi.testclient import TestClient

        from coordinare.config import CoordinareConfiguration
        from coordinare.config_validation import coerce_multi_symphony_raw
        from coordinare.dashboard import DashboardStore, create_dashboard_app

        raw = yaml.safe_load(config_path.read_text())
        raw["github_token"] = "ghp_fixturetoken"
        config_path.write_text(yaml.safe_dump(raw, sort_keys=False))
        cfg = CoordinareConfiguration(
            **coerce_multi_symphony_raw({**raw, "github_token": "ghp_fixturetoken"}),
        )
        daemon = MagicMock()
        daemon.state = {"coordinare_config": cfg, "config_version": 7}
        daemon.running = True
        app = create_dashboard_app(
            DashboardStore(), daemon, MagicMock(), MagicMock(), config_path=config_path,
        )
        return TestClient(app, base_url="http://127.0.0.1:8090")

    def test_a_stale_proposal_is_refused(self, temp_config_path) -> None:
        client = self._client(temp_config_path)
        stale = client.get("/api/config/all").json()["content_hashes"]["config_yaml"]

        # Somebody else edits the file after the proposal was built.
        temp_config_path.write_text(temp_config_path.read_text() + "\n# a concurrent edit\n")

        resp = client.put(
            "/api/config/global", json={"max_concurrent_cards": 5, "expected_hash": stale},
        )

        assert resp.status_code == 409, (
            "a proposal built against an older version of the file overwrote a "
            "concurrent edit"
        )
        assert "# a concurrent edit" in temp_config_path.read_text()

    def test_a_current_hash_is_accepted(self, temp_config_path) -> None:
        client = self._client(temp_config_path)
        current = client.get("/api/config/all").json()["content_hashes"]["config_yaml"]

        resp = client.put(
            "/api/config/global", json={"max_concurrent_cards": 5, "expected_hash": current},
        )

        assert resp.status_code == 200, resp.text
        import yaml

        assert yaml.safe_load(temp_config_path.read_text())["max_concurrent_cards"] == 5

    def test_without_a_hash_the_write_is_now_refused(self, temp_config_path) -> None:
        """Spec 157 changed this contract deliberately, so the test is inverted.

        It was written to pin the guard as *optional*, which is what spec 155 needed:
        the assistant could be guarded without changing any existing caller. Once
        every first-party caller sent a version, leaving it optional only preserved
        a way to silently lose someone's edit.
        """
        client = self._client(temp_config_path)

        resp = client.put("/api/config/global", json={"max_concurrent_cards": 6})

        assert resp.status_code == 428, resp.text
        import yaml

        assert yaml.safe_load(temp_config_path.read_text())["max_concurrent_cards"] != 6

    def test_expected_hash_is_not_written_into_the_config(self, temp_config_path) -> None:
        """It is a request header in spirit, not a setting."""
        client = self._client(temp_config_path)
        current = client.get("/api/config/all").json()["content_hashes"]["config_yaml"]

        client.put(
            "/api/config/global", json={"max_concurrent_cards": 7, "expected_hash": current},
        )

        import yaml

        assert "expected_hash" not in yaml.safe_load(temp_config_path.read_text())


class TestTheFeatureIsOffUntilItIsTurnedOn:
    """SC-005 / FR-013, FR-014.

    Off must mean the dashboard is the one it was before this feature existed —
    the routes are absent, not merely refusing. A feature that reads configuration
    and talks to a model endpoint should require a deliberate yes.
    """

    @staticmethod
    def _client(config_path, *, enabled: bool, backend=None):
        from unittest.mock import MagicMock

        import yaml
        from fastapi.testclient import TestClient

        from coordinare.config import CoordinareConfiguration
        from coordinare.config_validation import coerce_multi_symphony_raw
        from coordinare.dashboard import DashboardStore, create_dashboard_app

        raw = yaml.safe_load(config_path.read_text())
        raw["github_token"] = "ghp_fixturetoken"
        raw["config_assistant_enabled"] = enabled
        config_path.write_text(yaml.safe_dump(raw, sort_keys=False))
        cfg = CoordinareConfiguration(**coerce_multi_symphony_raw(raw))

        daemon = MagicMock()
        daemon.state = {
            "coordinare_config": cfg,
            "config_version": 7,
            "conducting_backend": backend,
        }
        daemon.running = True
        app = create_dashboard_app(
            DashboardStore(), daemon, MagicMock(), MagicMock(), config_path=config_path,
        )
        return TestClient(app, base_url="http://127.0.0.1:8090")

    def test_disabled_means_the_routes_do_not_exist(self, temp_config_path) -> None:
        client = self._client(temp_config_path, enabled=False)

        assert client.get("/api/assistant/status").status_code == 404
        assert client.post("/api/assistant/message", json={"message": "hi"}).status_code == 404

    def test_disabled_leaves_the_rest_of_the_dashboard_alone(self, temp_config_path) -> None:
        client = self._client(temp_config_path, enabled=False)

        assert client.get("/api/config/all").status_code == 200

    def test_enabled_without_a_backend_says_so_rather_than_failing(
        self, temp_config_path,
    ) -> None:
        """An operator who turned it on but configured no model gets an explanation."""
        client = self._client(temp_config_path, enabled=True, backend=None)

        status = client.get("/api/assistant/status").json()
        assert status["enabled"] is True
        assert status["ready"] is False
        assert status["reason"]

        turn = client.post("/api/assistant/message", json={"message": "hi"}).json()
        assert turn["proposal"] is None
        assert turn["error"]

    def test_a_turn_returns_a_proposal_the_operator_could_apply(self, temp_config_path) -> None:
        backend = ScriptedBackend(
            [
                {
                    "reply": "Two at a time is a reasonable start.",
                    "proposal": {
                        "section": "global",
                        "values": {"max_concurrent_cards": 2},
                        "reason": "so one stuck card does not idle the daemon",
                    },
                },
            ],
        )
        client = self._client(temp_config_path, enabled=True, backend=backend)

        body = client.post(
            "/api/assistant/message", json={"message": "how many cards at once?"},
        ).json()

        assert body["error"] is None, body["error"]
        assert body["proposal"]["section"] == "global"
        assert body["proposal"]["values"] == {"max_concurrent_cards": 2}

    def test_the_server_keeps_no_conversation(self, temp_config_path) -> None:
        """FR-015 — structural: the client carries the history, so there is nothing to keep.

        Asserted by sending two turns and checking the second prompt contains only
        what the client sent, not anything the server remembered on its own.
        """
        backend = ScriptedBackend(
            [{"reply": "one", "proposal": None}, {"reply": "two", "proposal": None}],
        )
        client = self._client(temp_config_path, enabled=True, backend=backend)

        client.post("/api/assistant/message", json={"message": "first question"})
        client.post("/api/assistant/message", json={"message": "second question"})

        assert "first question" not in backend.prompts[1], (
            "the server carried the previous turn forward on its own; conversation "
            "state must live only in the client"
        )

    def test_the_page_is_byte_identical_when_disabled(self, temp_config_path) -> None:
        """SC-005 taken literally: off means absent, not merely inert.

        The assistant is a whole extra page. Shipping it to every deployment and
        hiding it with CSS would have grown the shared page for operators who
        never enabled it — and the dashboard has a size budget precisely because
        that keeps happening.
        """
        from coordinare.dashboard import _DASHBOARD_HTML

        client = self._client(temp_config_path, enabled=False)
        served = client.get("/").text

        assert served == _DASHBOARD_HTML
        assert "assistant-log" not in served

    def test_the_panel_is_present_when_enabled(self, temp_config_path) -> None:
        client = self._client(temp_config_path, enabled=True, backend=ScriptedBackend())
        served = client.get("/").text

        assert "assistant-log" in served
        assert "assistantSend" in served

    def test_the_shared_page_stays_within_its_budget(self) -> None:
        """The existing budget test guards `_DASHBOARD_HTML`; this says why it still fits.

        The assistant's markup and script live in a fragment spliced in at serve
        time, so enabling the feature costs the operator who enabled it and nobody
        else.
        """
        from coordinare.dashboard import (
            _ASSISTANT_FRAGMENT,
            _DASHBOARD_HTML,
            DASHBOARD_HTML_BUDGET_BYTES,
        )

        assert len(_DASHBOARD_HTML.encode()) < DASHBOARD_HTML_BUDGET_BYTES
        assert len(_ASSISTANT_FRAGMENT.encode()) > 4000, (
            "the fragment looks empty — if the panel moved back into the shared "
            "page, the budget test will fail for a reason nobody will connect to this"
        )


class TestAFreshInstallIsToldWhatToDoFirst:
    """SC-007's testable half — the panel opens on what is missing.

    The half that depends on the model ("applying the proposals in order reaches a
    valid config") is not asserted here, because it would be a test of whichever
    endpoint the operator configured rather than of this code. Saying so beats a
    test that mocks the model into agreeing and calls that proof.
    """

    @staticmethod
    def _config(**overrides):
        import sys

        sys.path.insert(0, "tests/unit")
        from conftest import REPRESENTATIVE_CONFIG
        from coordinare.config import CoordinareConfiguration
        from coordinare.config_validation import coerce_multi_symphony_raw

        raw = {**REPRESENTATIVE_CONFIG, "github_token": "ghp_fixturetoken", **overrides}
        return CoordinareConfiguration(**coerce_multi_symphony_raw(raw))

    def test_no_model_endpoint_is_the_first_thing_raised(self) -> None:
        from coordinare.services.config_assistant import opening_guidance

        # The catalogs chain: endpoints <- model_endpoints <- modes. Clearing one
        # without the others is not a state coordinare can be in, so the test would
        # be asserting against a config that could never exist.
        opening = opening_guidance(self._config(endpoints=[], model_endpoints=[], modes=[]))

        assert "model" in opening.lower()

    def test_a_configured_install_gets_an_open_invitation(self) -> None:
        from coordinare.services.config_assistant import opening_guidance

        opening = opening_guidance(self._config())

        assert "ask me" in opening.lower()
        assert "you decide" in opening.lower(), (
            "the opening should say who applies changes; it is the whole posture"
        )

    def test_the_missing_endpoint_is_raised_before_the_missing_org(self) -> None:
        """Ordered by what blocks what, not by which field comes first in the file."""
        from coordinare.services.config_assistant import opening_guidance

        opening = opening_guidance(
            self._config(endpoints=[], model_endpoints=[], modes=[], github_org=""),
        )

        assert "model" in opening.lower(), (
            "an operator sent to configure a board first would hit the unreachable "
            "endpoint later, as a failure nobody warned them about"
        )

    def test_the_opening_reaches_the_panel(self, temp_config_path) -> None:
        client = TestTheFeatureIsOffUntilItIsTurnedOn._client(
            temp_config_path, enabled=True, backend=ScriptedBackend(),
        )

        assert client.get("/api/assistant/status").json()["opening"]

    def test_a_config_without_a_symphony_cannot_exist(self) -> None:
        """Which is why there is no guidance for one.

        Guidance for an unreachable state is worse than none: it implies to the next
        reader that an operator could be sitting in it. This pins the fact the
        omission rests on, so if validation ever loosens, the omission is revisited.
        """
        import pydantic

        with pytest.raises(pydantic.ValidationError):
            self._config(symphonies=[])

    def test_the_opening_never_offers_what_it_cannot_deliver(self) -> None:
        """Found by reading the two halves against each other, not by a failing test.

        The first-run guidance said "I will propose an endpoint for it", while
        ``validate_proposal`` rejects every proposal against a collection section —
        endpoints among them. A fresh-install operator would have been promised
        something on the opening line and told no only after answering.

        This checks the invariant rather than the sentence: guidance may not offer
        to propose against a section that proposals are rejected for.
        """
        from coordinare.services.config_assistant import (
            Proposal,
            opening_guidance,
            validate_proposal,
        )

        cfg = self._config(endpoints=[], model_endpoints=[], modes=[])
        opening = opening_guidance(cfg).lower()

        unproposable = [
            s
            for s in ("endpoints", "model_endpoints", "personas", "symphonies", "modes")
            if validate_proposal(cfg, Proposal(section=s, values={"x": 1})) is not None
        ]
        assert unproposable, "the premise changed: collections became proposable"

        for section in unproposable:
            offered = f"propose a{'n' if section[0] in 'aeiou' else ''} {section.rstrip('s')}"
            assert offered not in opening, (
                f"the opening offers to propose {section}, which is rejected as a "
                "collection — the operator finds out only after answering"
            )
        assert "i will propose an endpoint" not in opening


class TestAProposalIsCheckedBeforeTheOperatorSeesIt:
    """Review finding: field names existing is not the same as the values working.

    A model can offer ``log_level: "verbose"`` — a real field, a plausible value,
    and not one the schema accepts. Without checking, the operator reads a sensible
    diff, clicks Apply, and gets a 400 from the write endpoint. Validating early
    exists precisely so the button is not where errors surface (FR-004).
    """

    @staticmethod
    def _proposal(values):
        from coordinare.services.config_assistant import Proposal

        return Proposal(section="global", values=values)

    @pytest.mark.parametrize(
        "values",
        [
            pytest.param({"max_concurrent_cards": "a few"}, id="wrong-type"),
            pytest.param({"log_level": "verbose"}, id="not-in-the-pattern"),
            pytest.param({"max_concurrent_cards": 999999}, id="out-of-range"),
            pytest.param({"max_concurrent_cards": None}, id="null"),
        ],
    )
    def test_a_value_the_schema_rejects_never_reaches_apply(
        self, coordinare_config, values,
    ) -> None:
        from coordinare.services.config_assistant import validate_proposal

        rejection = validate_proposal(coordinare_config, self._proposal(values))

        assert rejection is not None, f"{values} would have been offered as appliable"

    def test_the_rejection_says_which_field_and_why(self, coordinare_config) -> None:
        """'1 validation error' tells an operator nothing they can act on."""
        from coordinare.services.config_assistant import validate_proposal

        rejection = validate_proposal(coordinare_config, self._proposal({"log_level": "verbose"}))

        assert "log_level" in rejection
        assert "pattern" in rejection.lower() or "match" in rejection.lower()

    @pytest.mark.parametrize(
        "values",
        [
            pytest.param({"max_concurrent_cards": 3}, id="in-range"),
            pytest.param({"log_level": "debug"}, id="in-the-pattern"),
        ],
    )
    def test_a_valid_value_is_still_offered(self, coordinare_config, values) -> None:
        from coordinare.services.config_assistant import validate_proposal

        assert validate_proposal(coordinare_config, self._proposal(values)) is None

    @pytest.mark.asyncio
    async def test_an_invalid_proposal_surfaces_as_an_error_not_a_proposal(
        self, coordinare_config,
    ) -> None:
        from coordinare.services.config_assistant import run_turn

        turn = await run_turn(
            message="set the log level to verbose",
            history=[],
            config=coordinare_config,
            backend=ScriptedBackend(
                [
                    {
                        "reply": "sure",
                        "proposal": {"section": "global", "values": {"log_level": "verbose"}},
                    },
                ],
            ),
        )

        assert turn.proposal is None
        assert turn.error and "log_level" in turn.error


class TestTheSecretsClaimIsStatedNarrowly:
    """Review finding: the guarantee was written more broadly than it holds.

    Config secrets are masked. Text the operator types is not, and cannot usefully
    be — filtering arbitrary prose for things that might be secrets would be both
    unreliable and a way to mangle legitimate questions. The risk in the original
    wording was that an operator would read "secrets do not reach the model" and
    paste a token to ask about it.
    """

    def test_the_panel_warns_before_the_operator_types(self) -> None:
        from coordinare.dashboard import _ASSISTANT_FRAGMENT

        fragment = _ASSISTANT_FRAGMENT.lower()

        assert "what you type" in fragment
        assert "do not paste" in fragment

    def test_the_module_does_not_overclaim(self) -> None:
        import coordinare.services.config_assistant as mod

        doc = " ".join((mod.__doc__ or "").split())

        assert "configuration secrets do not reach the model" in doc.lower(), (
            "the docstring should name what is actually guaranteed"
        )
        assert "typed" in doc.lower() or "types" in doc.lower(), (
            "and should say what is not"
        )

    @pytest.mark.asyncio
    async def test_typed_text_does_reach_the_model_which_is_why_it_is_stated(self) -> None:
        """Pins the behaviour the warning is about, so the two cannot drift apart."""
        from coordinare.services.config_assistant import run_turn

        backend = ScriptedBackend([{"reply": "ok", "proposal": None}])
        await run_turn(
            message="is my token ghp_typedbytheoperator in the right place?",
            history=[],
            config=TestSecretsNeverReachTheModel._config_holding_a_secret(),
            backend=backend,
        )

        assert "ghp_typedbytheoperator" in backend.prompts[0], (
            "if typed text stopped reaching the model, the warning is now wrong"
        )
