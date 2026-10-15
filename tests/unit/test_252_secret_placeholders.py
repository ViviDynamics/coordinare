"""#252: an unexpanded ${VAR} is refused in every secret field, at boot and at save.

#225 covered four fields by name. These tests cover every field typed as a secret anywhere in the
config model (nested in models, lists and dicts, Optional or not), and they find those fields with
their own walk of the annotations, so a secret added later is covered or fails here. Symphony
overrides are checked as the partial ProjectConfiguration they are merged into.
"""

from __future__ import annotations

import re
import types
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Union, get_args, get_origin

import pytest
import yaml
from pydantic import BaseModel, RootModel, SecretStr

from coordinare.config import CoordinareConfiguration, ProjectConfiguration, expand_config_text
from coordinare.config_validation import coerce_multi_symphony_raw
from coordinare.services.config_write_service import _validate_candidate
from coordinare.start_checks import (
    placeholder_problems,
    raw_placeholder_problems,
    secret_paths,
    validate_start_time,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_UNSET = "COORDINARE_252_UNSET_VAR"
_PLACEHOLDER = f"${{{_UNSET}}}"
_LIST_ITEM = "[]"
_DICT_VALUE = "{}"
_BASE: dict[str, Any] = {
    "github_org": "acme",
    "github_token": "ghp_fixturetoken",
    "human_reviewers": ["alice"],
    "symphonies": [{"name": "demo", "github_project_number": 100}],
}


@pytest.fixture(autouse=True)
def _no_placeholder_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test starts with the probe variable unset."""
    monkeypatch.delenv(_UNSET, raising=False)


def _independent_secret_paths(annotation: object, stack: frozenset[type] = frozenset()) -> Iterator[tuple[str, ...]]:
    """Walk the annotations on their own: every path to a SecretStr, through models, lists and dicts."""
    if isinstance(annotation, str):
        raise AssertionError(f"unresolved annotation {annotation!r}: the walk would miss what it hides")
    if annotation is SecretStr:
        yield ()
        return
    origin = get_origin(annotation)
    if origin is list:
        for sub in _independent_secret_paths(get_args(annotation)[0], stack):
            yield (_LIST_ITEM, *sub)
    elif origin is dict:
        for sub in _independent_secret_paths(get_args(annotation)[1], stack):
            yield (_DICT_VALUE, *sub)
    elif origin in (Union, types.UnionType):
        for arg in get_args(annotation):
            yield from _independent_secret_paths(arg, stack)
    elif isinstance(annotation, type) and issubclass(annotation, RootModel):
        yield from _independent_secret_paths(annotation.model_fields["root"].annotation, stack)
    elif isinstance(annotation, type) and issubclass(annotation, BaseModel) and annotation not in stack:
        for name, info in annotation.model_fields.items():
            for sub in _independent_secret_paths(info.annotation, stack | {annotation}):
                yield (name, *sub)


_WALKED = sorted(set(_independent_secret_paths(ProjectConfiguration)))


def _display(path: tuple[str, ...]) -> str:
    """The path as an operator reads it, with the first item of every list."""
    out = ""
    for segment in path:
        if segment == _LIST_ITEM:
            out += "[0]"
        else:
            out = f"{out}.{segment}" if out else segment
    return out


def _doc_for(path: tuple[str, ...], value: object) -> Any:
    """A minimal dict holding ``value`` at ``path``."""
    if not path:
        return value
    segment, rest = path[0], path[1:]
    if segment == _LIST_ITEM:
        return [_doc_for(rest, value)]
    return {segment: _doc_for(rest, value)}


def _multi(overrides: dict[str, Any]) -> dict[str, Any]:
    """A multi-symphony config in its on-disk (flat) shape, whose one symphony carries ``overrides``."""
    return {
        "github_org": "acme",
        "github_token": "ghp_fixturetoken",
        "human_reviewers": ["alice"],
        "symphonies": [{"name": "demo", "github_project_number": 100, "overrides": overrides}],
    }


# --- the walk and the coverage it drives ----------------------------------------------------


def test_the_independent_walk_finds_the_known_secret_fields() -> None:
    walked = set(_WALKED)
    assert ("github_token",) in walked
    assert ("dashboard_auth_token",) in walked
    assert ("dashboard_oidc", "client_secret") in walked
    assert ("webhooks", "secret") in walked
    assert ("notifications", "channels", _LIST_ITEM, "smtp_password") in walked
    assert ("notifications", "channels", _LIST_ITEM, "webhook_url") in walked
    assert ("performer_endpoints", _LIST_ITEM, "auth_token") in walked


def test_production_coverage_is_exactly_the_secret_fields_the_walk_finds() -> None:
    assert sorted(secret_paths(ProjectConfiguration)) == _WALKED


@pytest.mark.parametrize("path", _WALKED, ids=[".".join(p) for p in _WALKED])
def test_every_secret_field_refuses_an_unset_placeholder_at_save(path: tuple[str, ...]) -> None:
    problems = raw_placeholder_problems(_doc_for(path, _PLACEHOLDER))
    assert [field for field, _ in problems] == [_display(path)]
    assert _UNSET in problems[0][1]


@pytest.mark.parametrize("path", _WALKED, ids=[".".join(p) for p in _WALKED])
def test_every_secret_field_refuses_an_unset_placeholder_in_a_symphony_override(path: tuple[str, ...]) -> None:
    problems = raw_placeholder_problems(_multi(_doc_for(path, _PLACEHOLDER)))
    assert [field for field, _ in problems] == [f"symphonies[0].overrides.{_display(path)}"]
    assert _UNSET in problems[0][1]


@pytest.mark.parametrize("path", _WALKED, ids=[".".join(p) for p in _WALKED])
def test_every_secret_field_accepts_a_variable_that_is_set(
    path: tuple[str, ...], monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(_UNSET, "a-value-from-the-environment")
    assert raw_placeholder_problems(_doc_for(path, _PLACEHOLDER)) == []
    assert raw_placeholder_problems(_multi(_doc_for(path, _PLACEHOLDER))) == []


@pytest.mark.parametrize("path", _WALKED, ids=[".".join(p) for p in _WALKED])
def test_every_secret_field_holding_a_secret_value_is_checked_by_its_unwrapped_value(path: tuple[str, ...]) -> None:
    """The boot check reads typed values, so a SecretStr is unwrapped before the placeholder test."""
    problems = placeholder_problems(ProjectConfiguration, _doc_for(path, SecretStr(_PLACEHOLDER)))
    assert [field for field, _ in problems] == [_display(path)]


# --- the shape of a placeholder ---------------------------------------------------------------


def _refused(value: object) -> list[tuple[str, str]]:
    return placeholder_problems(ProjectConfiguration, {"github_token": value})


def test_only_a_braced_name_is_a_placeholder() -> None:
    assert _refused("${COORDINARE_252_UNSET_VAR}")
    assert _refused("$COORDINARE_252_UNSET_VAR") == []
    assert _refused("pa$$word") == []
    assert _refused("pa$word-and-more") == []
    assert _refused("${9starts_with_a_digit}") == []
    assert _refused("${has a space}") == []
    assert _refused("plain-value") == []
    assert _refused(None) == []
    assert _refused(42) == []


def test_a_placeholder_inside_a_longer_value_is_refused() -> None:
    problems = _refused(f"https://hooks.example/x/{_PLACEHOLDER}/y")
    assert [field for field, _ in problems] == ["github_token"]
    assert _UNSET in problems[0][1]


def test_the_raw_check_accepts_a_placeholder_whose_variable_is_set(monkeypatch: pytest.MonkeyPatch) -> None:
    """252 review: the raw check reads the text as written, so what a set variable holds is never inspected."""
    monkeypatch.setenv(_UNSET, "ab${OTHER}cd")
    assert raw_placeholder_problems({"github_token": _PLACEHOLDER}) == []


def test_the_loaded_value_fallback_refuses_braced_text_while_the_name_is_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without the text, the loaded value cannot be told from a placeholder, so the fallback refuses it."""
    monkeypatch.setenv(_UNSET, "ab${OTHER}cd")
    problems = placeholder_problems(ProjectConfiguration, {"github_token": SecretStr("ab${OTHER}cd")})
    assert problems
    assert "environment variable OTHER" in problems[0][1]


def test_the_message_names_the_field_and_the_variable() -> None:
    problems = _refused(_PLACEHOLDER)
    assert problems[0][0] == "github_token"
    assert "github_token" in problems[0][1]
    assert _UNSET in problems[0][1]


# --- the entry points: boot, and save ----------------------------------------------------------


def test_boot_refuses_an_unset_smtp_password_and_names_the_variable() -> None:
    raw = {
        "github_org": "acme",
        "github_token": "ghp_fixturetoken",
        "human_reviewers": ["alice"],
        "notifications": {"channels": [{"name": "mail", "type": "email", "smtp_host": "smtp.example", "smtp_recipient": "a@example", "smtp_password": _PLACEHOLDER}]},
    }
    config = ProjectConfiguration(**raw)
    with pytest.raises(ValueError, match=_UNSET):
        validate_start_time(config)


def test_boot_refuses_an_unset_variable_in_a_symphony_override_and_names_the_path() -> None:
    raw = _multi({"notifications": {"channels": [{"name": "mail", "type": "email", "smtp_host": "smtp.example", "smtp_recipient": "a@example", "smtp_password": _PLACEHOLDER}]}})
    coordinare = CoordinareConfiguration(**coerce_multi_symphony_raw(raw))
    with pytest.raises(ValueError, match=r"symphonies\[0\]\.overrides\.notifications\.channels\[0\]\.smtp_password"):
        validate_start_time(coordinare.global_config, coordinare)


def test_boot_accepts_a_symphony_override_once_its_variable_is_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_UNSET, "a-password")
    raw = _multi({"notifications": {"channels": [{"name": "mail", "type": "email", "smtp_host": "smtp.example", "smtp_recipient": "a@example", "smtp_password": _PLACEHOLDER}]}})
    coordinare = CoordinareConfiguration(**coerce_multi_symphony_raw(raw))
    validate_start_time(coordinare.global_config, coordinare)


def test_save_refuses_an_unset_slack_webhook_and_names_the_variable() -> None:
    raw = {
        **_BASE,
        "notifications": {"channels": [{"name": "team", "type": "slack", "webhook_url": _PLACEHOLDER}]},
    }
    errors = _validate_candidate(raw)
    assert errors
    assert errors[0].key == "notifications.channels[0].webhook_url"
    assert _UNSET in errors[0].message
    assert "notifications.channels[0].webhook_url" in errors[0].message


def test_save_refuses_an_unset_performer_auth_token_and_names_the_path() -> None:
    raw = {
        **_BASE,
        "performer_endpoints": [{
            "id": "remote", "mode": "persistent", "roles": ["implementer"],
            "endpoint": "http://performer.example:8080/", "image": "performer:latest",
            "auth_token": _PLACEHOLDER,
        }],
    }
    errors = _validate_candidate(raw)
    assert errors
    assert "performer_endpoints[0].auth_token" in errors[0].message
    assert _UNSET in errors[0].message


def test_save_refuses_an_unset_symphony_override_and_names_the_symphony() -> None:
    raw = _multi({"notifications": {"channels": [{"name": "mail", "type": "email", "smtp_host": "smtp.example", "smtp_recipient": "a@example", "smtp_password": _PLACEHOLDER}]}})
    errors = _validate_candidate(raw)
    assert errors
    assert "symphonies[0].overrides.notifications.channels[0].smtp_password" in errors[0].message


# --- configs that must not be refused ----------------------------------------------------------

_EXAMPLES = sorted(_REPO_ROOT.glob("config.example*.yaml"))


@pytest.mark.parametrize("example", _EXAMPLES, ids=[p.name for p in _EXAMPLES])
def test_the_example_configs_have_no_unexpanded_secret_with_their_variables_set(
    example: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    text = example.read_text()
    for name in set(re.findall(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", text)):
        monkeypatch.setenv(name, "set-for-the-example-check")
    assert raw_placeholder_problems(yaml.safe_load(text)) == []
    assert placeholder_problems(ProjectConfiguration, yaml.safe_load(expand_config_text(text))) == []
