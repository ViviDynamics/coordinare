"""148: client secrets given as a flag or an argument, and linear time on adversarial input.

The command forms are found by one linear pass over the words of a line (not by a
pattern that scans to the end of the line from every program name, which is what
made ``docker login`` repeated a quadratic cost). The timing assertions compare a
large input with a ten times smaller one, so a slow CI machine does not move them.
"""
from __future__ import annotations

import time
from collections.abc import Callable

import pytest
from performer import models as performer_models

from coordinare.lib.redaction import redact_command_text

# (command, the literal that must not survive)
CLIENT_SECRETS = [
    ("redis-cli -a hunter2 ping", "hunter2"),
    ("openssl enc -aes-256-cbc -pass pass:hunter2 -in a -out b", "hunter2"),
    ("openssl enc -aes-256-cbc -passout pass:hunter4 -out b", "hunter4"),
    ("htpasswd -b .htpasswd alice hunter5", "hunter5"),
    ("htpasswd -c -b .htpasswd alice hunter6", "hunter6"),
    ("sqlcmd -S host -U sa -P hunter7 -Q 'select 1'", "hunter7"),
    ("docker login -p hunter8 registry.example.com", "hunter8"),
    ("sshpass -p hunter9 ssh host uptime", "hunter9"),
    ("aws configure set aws_secret_access_key S3CRETVALUE", "S3CRETVALUE"),
    ("gh secret set DEPLOY_KEY --body DeployVal55", "DeployVal55"),
    ('sshpass -p "two words" ssh host', "two words"),
]


@pytest.mark.parametrize(("command", "secret"), CLIENT_SECRETS)
def test_a_client_secret_is_redacted_on_both_redactors(command: str, secret: str) -> None:
    assert secret not in redact_command_text(command)
    assert secret not in performer_models.redact_command(command)


def test_a_flag_keeps_its_space_before_the_redaction() -> None:
    assert redact_command_text("docker login -p hunter8 reg") == "docker login -p [REDACTED] reg"
    assert redact_command_text("sshpass -p hunter9 ssh host") == "sshpass -p [REDACTED] ssh host"


def test_a_pass_prefix_is_redacted_and_the_env_form_is_not() -> None:
    assert redact_command_text("openssl enc -pass pass:hunter2 -in a") == "openssl enc -pass pass:[REDACTED] -in a"
    assert redact_command_text("openssl enc -pass env:MYPW -in a") == "openssl enc -pass env:MYPW -in a"


def test_a_command_break_ends_the_client_context() -> None:
    """`docker login`, then a new command: its -p is not docker's."""
    text = "docker login reg && echo -p keepme"
    assert "keepme" in redact_command_text(text)


def test_a_newline_ends_the_client_context() -> None:
    text = "docker login reg\nprint -p keepme"
    assert "keepme" in redact_command_text(text)


CLIENT_SCAN_UNITS = [
    "docker login -q ",   # armed, never a -p: the no-match case
    "sshpass -q ",        # same, a different client
    "docker login -p x ",  # every repeat matches: the matching case
    "htpasswd -b f u p ",  # a positional count per repeat
    "gh secret set X ",   # armed by gh, no --body
    "aws configure set k ",  # armed by aws, no secret-like key
]


def _best_of(function: Callable[[str], str], text: str, runs: int = 3) -> float:
    best = float("inf")
    for _ in range(runs):
        start = time.perf_counter()
        function(text)
        best = min(best, time.perf_counter() - start)
    return best


@pytest.mark.parametrize("unit", CLIENT_SCAN_UNITS)
def test_repeated_client_starts_scan_linearly(unit: str) -> None:
    """A 1 MB line against 100 KB: linear is about tenfold, quadratic a hundredfold."""
    small = unit * (100_000 // len(unit))
    large = unit * (1_000_000 // len(unit))
    growth = _best_of(redact_command_text, large) / _best_of(redact_command_text, small)
    assert growth < 30, f"1 MB took {growth:.1f}x the 100 KB time for {unit!r}"


def test_repeated_docker_login_with_secrets_redacts_every_one_at_a_megabyte() -> None:
    out = redact_command_text("docker login -p hunter8 " * (1_000_000 // len("docker login -p hunter8 ")))
    assert "hunter8" not in out
    assert out.count("[REDACTED]") == out.count("docker login -p") == 1_000_000 // len("docker login -p hunter8 ")
