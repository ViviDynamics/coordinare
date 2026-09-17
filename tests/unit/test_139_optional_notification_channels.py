"""Spec 139 / issue #187 — notification channels are genuinely optional.

A developer disabled Slack during setup because it "wasn't working", then could
not tell whether agents were stuck. The cause was not delivery, which already has
retries and a circuit breaker. It was **config load**: a Slack channel without its
``webhook_url`` fails validation, so coordinare does not start at all. Deleting the
channel is the obvious fix, and that is what leaves the stall signal with nowhere
to go.

Notifications are non-essential — coordinare's work is unaffected by their absence —
so a misconfiguration here degrades that channel rather than stopping the daemon.
That reasoning does **not** extend to GitHub, board or model-endpoint config, where
proceeding would do the wrong work rather than merely tell nobody.

``config validate`` keeps reporting the misconfiguration, so the tool for finding
config problems is not blunted along with the daemon.
"""

from __future__ import annotations

import pytest
import yaml
from pydantic import ValidationError

from coordinare.services.notification_config import (
    SkippedChannel,
    describe_notification_posture,
    sanitize_notifications,
)

SLACK_SECRET = "https://hooks.slack.example/SUPER-SECRET-PATH"


def _raw(**notifications):
    return {
        "github_org": "ViviDynamics",
        "project_name": "p",
        "github_project_number": 1,
        "human_reviewers": ["someone"],
        "github_token": "t",
        "notifications": notifications,
    }


class TestAHalfConfiguredChannelDoesNotStopTheDaemon:
    """US1 — the reported failure."""

    def test_a_slack_channel_without_its_webhook_is_dropped_not_fatal(self) -> None:
        raw, skipped = sanitize_notifications(
            _raw(channels=[{"name": "team-slack", "type": "slack"}]),
        )
        assert raw["notifications"]["channels"] == []
        assert [s.name for s in skipped] == ["team-slack"]

    def test_the_reason_names_the_channel_and_what_is_missing(self) -> None:
        """FR-003 — a silent drop trades one invisible problem for another.

        The failure this replaces was at least loud. What follows it has to be
        discoverable without reading the config file.
        """
        _, skipped = sanitize_notifications(
            _raw(channels=[{"name": "team-slack", "type": "slack"}]),
        )
        reason = skipped[0].reason
        assert "webhook_url" in reason, f"the reason must name the missing field: {reason}"

    def test_the_sanitized_config_actually_loads(self) -> None:
        """The point of the exercise, stated end to end.

        Before this, the same input raised during ``ProjectConfiguration`` construction
        and coordinare never started.
        """
        from coordinare.config import ProjectConfiguration

        raw = _raw(channels=[{"name": "team-slack", "type": "slack"}])
        with pytest.raises(ValidationError):
            ProjectConfiguration(**raw)

        sanitized, _ = sanitize_notifications(raw)
        config = ProjectConfiguration(**sanitized)
        assert config.notifications.channels == []

    def test_an_email_channel_missing_its_recipient_is_treated_the_same(self) -> None:
        """Not a Slack-specific patch: any unusable channel degrades."""
        _, skipped = sanitize_notifications(
            _raw(channels=[{"name": "ops-mail", "type": "email", "smtp_host": "mail.example"}]),
        )
        assert [s.name for s in skipped] == ["ops-mail"]

    def test_a_usable_channel_survives_untouched(self) -> None:
        """FR-005 — nothing about a working setup may change."""
        raw = _raw(channels=[{"name": "team-slack", "type": "slack", "webhook_url": SLACK_SECRET}])
        sanitized, skipped = sanitize_notifications(raw)
        assert skipped == []
        assert sanitized["notifications"]["channels"][0]["webhook_url"] == SLACK_SECRET

    def test_a_mixed_configuration_keeps_what_works(self) -> None:
        raw = _raw(
            channels=[
                {"name": "good", "type": "slack", "webhook_url": SLACK_SECRET},
                {"name": "bad", "type": "slack"},
            ],
        )
        sanitized, skipped = sanitize_notifications(raw)
        assert [c["name"] for c in sanitized["notifications"]["channels"]] == ["good"]
        assert [s.name for s in skipped] == ["bad"]


class TestADanglingRoutingEntry:
    """US1 — the other way a half-finished setup stops the daemon."""

    def test_an_entry_naming_a_channel_that_never_existed_is_ignored(self) -> None:
        raw, _ = sanitize_notifications(
            _raw(channels=[], routing=[{"event_type": "card_stuck", "channels": ["ghost"]}]),
        )
        assert raw["notifications"]["routing"] == []

    def test_an_entry_naming_a_channel_that_was_just_dropped_is_ignored(self) -> None:
        """The two cases differ in cause, and the operator should be able to tell.

        A typo'd channel name and a channel that failed to configure need different
        fixes, so the message distinguishes them.
        """
        raw, skipped = sanitize_notifications(
            _raw(
                channels=[{"name": "team-slack", "type": "slack"}],
                routing=[{"event_type": "card_stuck", "channels": ["team-slack"]}],
            ),
        )
        assert raw["notifications"]["routing"] == []
        assert any("team-slack" in s.reason or s.name == "team-slack" for s in skipped)

    def test_an_entry_keeps_the_channels_that_are_usable(self) -> None:
        raw, _ = sanitize_notifications(
            _raw(
                channels=[{"name": "good", "type": "slack", "webhook_url": SLACK_SECRET}],
                routing=[{"event_type": "card_stuck", "channels": ["good", "ghost"]}],
            ),
        )
        entry = raw["notifications"]["routing"][0]
        assert entry["channels"] == ["good"], "a usable channel must survive a bad sibling"

    def test_the_sanitized_config_loads(self) -> None:
        from coordinare.config import ProjectConfiguration

        raw = _raw(channels=[], routing=[{"event_type": "card_stuck", "channels": ["ghost"]}])
        with pytest.raises(ValidationError):
            ProjectConfiguration(**raw)
        sanitized, _ = sanitize_notifications(raw)
        assert ProjectConfiguration(**sanitized).notifications.routing == []


class TestConfigValidateStaysStrict:
    """FR-004 — the half that keeps this honest.

    Deleting the validation rule would fix the daemon and blind the linter in the
    same move, trading a loud failure for a silent typo.

    **These asserted only ``result.passed`` at first, which proved nothing.** The
    helper drops ``github_token`` to keep a secret out of the temp file, so
    ``passed`` is False whatever the notifications say — the assertion held
    identically with the validation deleted. They now assert the *specific* error
    is present, and a control case pins that the assertion can fail.
    """

    @staticmethod
    def _errors(tmp_path, notifications) -> list:
        from coordinare.config_validation import validate_config

        path = tmp_path / "config.yaml"
        raw = _raw(**notifications)
        raw.pop("github_token", None)  # keep the file free of secrets
        path.write_text(yaml.safe_dump(raw))
        return list(validate_config(path).errors)

    @staticmethod
    def _notification_errors(errors) -> list:
        return [e for e in errors if "notifications" in e.field_path]

    def test_a_half_configured_channel_is_still_an_error(self, tmp_path) -> None:
        errors = self._errors(tmp_path, {"channels": [{"name": "team-slack", "type": "slack"}]})
        notification_errors = self._notification_errors(errors)
        assert notification_errors, (
            f"config validate must still report the channel the daemon now tolerates. "
            f"Errors seen: {[e.field_path for e in errors]}"
        )
        assert any("webhook_url" in str(e.fix_hint) for e in notification_errors)

    def test_a_dangling_routing_entry_is_still_an_error(self, tmp_path) -> None:
        errors = self._errors(
            tmp_path,
            {"channels": [], "routing": [{"event_type": "card_stuck", "channels": ["ghost"]}]},
        )
        notification_errors = self._notification_errors(errors)
        assert notification_errors, (
            f"the dangling routing entry was not reported. Errors seen: "
            f"{[e.field_path for e in errors]}"
        )
        assert any("ghost" in str(e.fix_hint) for e in notification_errors)

    def test_the_control_case_produces_no_notification_error(self, tmp_path) -> None:
        """Proof the two assertions above can fail.

        Without this, "a notification error is present" is indistinguishable from
        "some error is present", which is exactly how the earlier version passed
        while testing nothing.
        """
        errors = self._errors(tmp_path, {"channels": [], "routing": []})
        assert not self._notification_errors(errors), (
            "a valid notifications block still produced a notification error, so the "
            "assertions above would hold regardless of the validation under test"
        )


class TestTheStartupPostureLine:
    """US2 — silence must be distinguishable from health."""

    def test_it_names_active_channels(self) -> None:
        posture = describe_notification_posture(active=["team-slack"], skipped=[])
        assert "team-slack" in posture["active_channels"]

    def test_it_names_skipped_channels_and_why(self) -> None:
        posture = describe_notification_posture(
            active=[], skipped=[SkippedChannel(name="team-slack", reason="missing webhook_url")],
        )
        assert "team-slack" in str(posture["skipped_channels"])
        assert "webhook_url" in str(posture["skipped_channels"])

    def test_with_no_channels_it_says_where_stall_signals_will_appear(self) -> None:
        """The developer's actual question: is anything watching?"""
        posture = describe_notification_posture(active=[], skipped=[])
        summary = posture["summary"].lower()
        assert "no notification channels" in summary
        assert "log" in summary, (
            "with nothing configured, the line must name the surface that still works"
        )

    def test_no_configured_secret_reaches_the_line(self) -> None:
        """FR-007 — exercised along the path a secret could actually travel.

        The first version asserted a constant was absent from output built from
        unrelated inputs, so it passed no matter what the function did. Injecting
        the secret straight into the arguments is the opposite mistake: the
        function renders what it is given, so that only proves it does not
        sanitise, which it is not trying to do.

        What matters is the chain. A channel carrying a real webhook is rejected,
        the reason is derived from that rejection, and the posture line is built
        from it. If any link picked up the value — pydantic's error carries the
        offending ``input`` for some error types — it would surface here.
        """
        from coordinare.services.notification_config import _channel_error

        channel = {
            "name": "team-slack",
            "type": "slack",
            "webhook_url": SLACK_SECRET,
            "rate_limit": "not-a-number",  # rejected for a reason unrelated to the secret
        }
        reason = _channel_error(channel)
        assert reason, "this channel should have been rejected"

        posture = describe_notification_posture(
            active=[], skipped=[SkippedChannel(name=str(channel["name"]), reason=reason)],
        )
        rendered = str(posture)
        assert "SUPER-SECRET-PATH" not in rendered, (
            f"a credential reached the posture line, which is logged at startup: {rendered}"
        )

    def test_a_skip_reason_does_not_carry_the_rejected_value(self) -> None:
        """The route a leak would actually take.

        ``_channel_error`` builds its reason from pydantic's ``msg``, not its
        ``input``. That is correct, but nothing asserted it — so a change to read
        the richer field would leak a webhook URL into a log line without any test
        noticing.
        """
        from coordinare.services.notification_config import _channel_error

        reason = _channel_error(
            {
                "name": "c",
                "type": "slack",
                "webhook_url": SLACK_SECRET,
                "rate_limit": "not-a-number",
            },
        )
        assert reason, "this channel should have been rejected"
        assert SLACK_SECRET not in reason, f"the skip reason leaks the webhook URL: {reason}"

    def test_it_is_one_line(self) -> None:
        posture = describe_notification_posture(active=[], skipped=[])
        assert "\n" not in posture["summary"]


class TestZeroChannelsRemainsClean:
    """FR-011 — already true, locked so it stays true."""

    def test_no_notifications_block_at_all(self) -> None:
        from coordinare.config import ProjectConfiguration

        raw = _raw()
        raw.pop("notifications")
        sanitized, skipped = sanitize_notifications(raw)
        assert skipped == []
        config = ProjectConfiguration(**sanitized)
        assert config.notifications.channels == []

    def test_an_explicitly_empty_notifications_block(self) -> None:
        from coordinare.config import ProjectConfiguration

        sanitized, skipped = sanitize_notifications(_raw(channels=[], routing=[]))
        assert skipped == []
        assert ProjectConfiguration(**sanitized).notifications.channels == []


class TestTheDaemonToleratesOnlyNotificationErrors:
    """FR-001/FR-002 at the layer that actually exits.

    The daemon runs ``validate_config`` and exits before it ever constructs the
    typed config, so tolerating unusable channels downstream would have changed
    nothing. The decision has to be made against the validation result.
    """

    @staticmethod
    def _is_notification_error(path: str) -> bool:
        from coordinare.__main__ import _is_notification_error

        return _is_notification_error(path)

    def test_a_channel_error_is_recognised_as_notification_scoped(self) -> None:
        assert self._is_notification_error("global_config.notifications.channels[0]")

    def test_a_routing_error_is_recognised(self) -> None:
        assert self._is_notification_error("global_config.notifications.routing")

    @pytest.mark.parametrize(
        "path",
        [
            "global_config.github_token",
            "github_org",
            "endpoints[0].base_url",
            "global_config.notifications_webhook",  # near-miss, not the block
        ],
    )
    def test_everything_else_still_stops_the_daemon(self, path: str) -> None:
        """The bound on this whole change.

        Broken GitHub or endpoint config would have coordinare do the *wrong* work
        while looking healthy. Only telling nobody is recoverable.
        """
        assert not self._is_notification_error(path)

    def test_it_matches_on_the_path_not_the_message(self) -> None:
        """Messages are prose and get reworded; paths are structural."""
        assert not self._is_notification_error("some.field.about.notifications_stuff")


class TestTheStallIsLoggedWithNothingElseConfigured:
    """US3 — the surface that survives everything being switched off."""

    def test_the_stall_log_is_emitted_before_the_feed_and_the_dispatch(self) -> None:
        """Ordering is the requirement, not a detail.

        The activity-feed record needs the dashboard and the notification needs a
        channel. The log has to happen before *both*, or it inherits their
        preconditions and stops being the always-available surface.

        An earlier version of this test checked only the feed, despite being named
        for the dispatch — so moving the dispatch earlier would not have failed it.
        """
        import inspect

        from coordinare import daemon

        source = inspect.getsource(daemon)
        log_at = source.index("if should_log_stall(")
        feed_at = source.index('_alog = self._state.get("activity_log")')
        dispatch_at = source.index("event_type=EventType.card_stuck")

        assert log_at < feed_at, "the log must precede the activity feed"
        assert log_at < dispatch_at, "the log must precede the notification dispatch"

    def test_the_log_carries_what_an_operator_needs(self) -> None:
        import inspect

        from coordinare import daemon

        source = inspect.getsource(daemon)
        at = source.index('"card_stuck"')
        block = source[at : at + 700]
        for field in ("card_id", "stage", "stuck_minutes"):
            assert field in block, f"the stall log must name {field}"

    def test_the_dedup_key_matches_the_notification_path(self) -> None:
        """One policy, not two.

        Two independent dedup policies for the same event disagree eventually, and
        the disagreement shows up as either log spam or a missing warning.
        """
        import inspect

        from coordinare import daemon

        source = inspect.getsource(daemon)
        key_expr = "stuck:{_card.get('id', '')}:{_stuck_phase}"
        assert source.count(key_expr) >= 2, (
            "the stall log and the notification dispatch must derive the same key"
        )


class TestTheDocumentationSaysWhereAlertsGo:
    """FR-019 — the claim must not drift from the behaviour.

    An operator deciding whether it is safe to run without Slack needs the answer
    written down, not inferable from source.
    """

    @staticmethod
    def _doc() -> str:
        from pathlib import Path

        return Path("docs/quickstart.md").read_text().lower()

    def test_it_says_channels_are_optional(self) -> None:
        assert "notifications are optional" in self._doc()

    def test_it_names_the_always_available_surface(self) -> None:
        doc = self._doc()
        assert "card_stuck" in doc
        assert "log" in doc

    def test_it_says_config_validate_still_reports_the_problem(self) -> None:
        """Otherwise the relaxation reads as "coordinare stopped caring"."""
        assert "config validate" in self._doc()

    def test_it_states_the_bound_on_what_degrades(self) -> None:
        """The distinction that stops this becoming a general licence."""
        doc = self._doc()
        assert "refuse to start" in doc or "refuses to start" in doc


class TestTheStallDedupPolicy:
    """The policy itself, tested directly rather than asserted on source text.

    It had two wrong versions before this one, and neither was caught by reading:

    * remember keys, clear the dict when it grows. Once live stalls exceeded the
      bound, every remembered key became loggable again on the next cycle.
    * evict the oldest key instead. Same outcome by a different route — the
      evicted key logs, which evicts the next, and the cascade repeats each cycle.

    Both look right. A simulation shows 1539 lines where 513 were wanted.
    """

    @staticmethod
    def _policy():
        from coordinare.daemon import should_log_stall

        return should_log_stall

    def test_the_first_sighting_of_a_stall_is_logged(self) -> None:
        seen: dict[str, float] = {}
        assert self._policy()(seen, "stuck:1:impl", 0.0)

    def test_the_same_stall_is_quiet_within_the_cooldown(self) -> None:
        seen: dict[str, float] = {}
        policy = self._policy()
        policy(seen, "stuck:1:impl", 0.0)
        assert not policy(seen, "stuck:1:impl", 60.0)
        assert not policy(seen, "stuck:1:impl", 3599.0)

    def test_a_stall_that_persists_reminds_after_the_cooldown(self) -> None:
        """One line and then permanent silence would be its own failure."""
        seen: dict[str, float] = {}
        policy = self._policy()
        policy(seen, "stuck:1:impl", 0.0)
        assert policy(seen, "stuck:1:impl", 3600.0)

    def test_a_different_phase_is_a_different_stall(self) -> None:
        seen: dict[str, float] = {}
        policy = self._policy()
        policy(seen, "stuck:1:implementing", 0.0)
        assert policy(seen, "stuck:1:reviewing", 1.0)

    def test_more_live_stalls_than_the_bound_does_not_cause_a_burst(self) -> None:
        """The defect both earlier versions had, pinned.

        With more distinct stalls than the sweep threshold, a count-based bound
        re-logs everything every cycle. Time-based bounding is bounded by the
        board instead.
        """
        from coordinare.daemon import _MAX_LOGGED_STALLS

        policy = self._policy()
        seen: dict[str, float] = {}
        stalls = _MAX_LOGGED_STALLS + 1
        logged = 0
        for cycle in range(3):
            now = 30.0 * (cycle + 1)  # cycles well inside the cooldown
            for i in range(stalls):
                if policy(seen, f"stuck:card{i}:impl", now):
                    logged += 1

        assert logged == stalls, (
            f"{logged} log lines for {stalls} stalls over 3 cycles; expected one each. "
            "A count-based bound produces roughly one burst per cycle instead."
        )

    def test_memory_does_not_grow_without_limit(self) -> None:
        """Bounded by how many cards can be stuck within the cooldown."""
        from coordinare.daemon import _MAX_LOGGED_STALLS, _STALL_LOG_COOLDOWN_SECONDS

        policy = self._policy()
        seen: dict[str, float] = {}
        for i in range(_MAX_LOGGED_STALLS * 3):
            # Each key well past the previous one's cooldown, so all are expired.
            policy(seen, f"stuck:card{i}:impl", i * _STALL_LOG_COOLDOWN_SECONDS * 2)
        assert len(seen) <= _MAX_LOGGED_STALLS + 1, f"unbounded growth: {len(seen)} keys"


class TestSymphonyOverridesAreSanitizedToo:
    """The hole that made the whole feature half-work on multi-symphony configs.

    ``overrides`` is an untyped ``dict[str, Any]``, so a bad channel there passes
    the daemon's tolerance check *and* builds ``CoordinareConfiguration`` without
    complaint — then raises from ``SymphonyConfig.effective_config``, far from the
    cause and long after the point the daemon decided it was fine.
    """

    @staticmethod
    def _multi(channel):
        return {
            "global_config": {
                "github_org": "V",
                "project_name": "p",
                "github_project_number": 1,
                "human_reviewers": ["x"],
                "github_token": "t",
            },
            "symphonies": [
                {
                    "name": "web",
                    "github_project_number": 2,
                    "overrides": {"notifications": {"channels": [channel]}},
                },
            ],
        }

    def test_a_bad_channel_in_a_symphony_override_is_dropped(self) -> None:
        raw, skipped = sanitize_notifications(self._multi({"name": "team-slack", "type": "slack"}))
        assert raw["symphonies"][0]["overrides"]["notifications"]["channels"] == []
        assert skipped, "the skipped channel must be reported, not silently dropped"

    def test_the_reason_says_which_symphony(self) -> None:
        """Two symphonies can each have a channel of the same name."""
        _, skipped = sanitize_notifications(self._multi({"name": "team-slack", "type": "slack"}))
        assert "web" in skipped[0].name, f"cannot tell which symphony: {skipped[0].name}"

    def test_the_symphony_config_then_resolves(self) -> None:
        """The end the operator actually reaches."""
        from coordinare.config import CoordinareConfiguration

        raw, _ = sanitize_notifications(self._multi({"name": "team-slack", "type": "slack"}))
        coordinare_config = CoordinareConfiguration(**raw)
        coordinare_config.symphonies[0].effective_config(coordinare_config.global_config)

    def test_a_good_channel_in_an_override_survives(self) -> None:
        good = {"name": "team-slack", "type": "slack", "webhook_url": SLACK_SECRET}
        raw, skipped = sanitize_notifications(self._multi(good))
        assert skipped == []
        assert raw["symphonies"][0]["overrides"]["notifications"]["channels"] == [good]
