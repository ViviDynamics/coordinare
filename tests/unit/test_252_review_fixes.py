"""#252 review: URL and environment values, the boot exit, empty variables, walk edge cases, error keys.

The oracle for what counts as a secret, a URL or an environment value is pydantic's own JSON schema,
not the annotation walk the daemon uses, so a blind spot in one does not hide in the other.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Annotated, Any

import pytest
import yaml
from pydantic import BaseModel, Field, RootModel, SecretStr, TypeAdapter

from coordinare.config import CoordinareConfiguration, ProjectConfiguration
from coordinare.config_validation import coerce_multi_symphony_raw
from coordinare.services.config_write_service import _validate_candidate
from coordinare.start_checks import (
    TEXT_FIELD_NAMES,
    TEXT_FIELD_SUFFIXES,
    StartCheckError,
    placeholder_problems,
    raw_placeholder_problems,
    secret_paths,
    validate_start_time,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_UNSET = "COORDINARE_252_UNSET_VAR"
_EMPTY = "COORDINARE_252_EMPTY_VAR"
_PLACEHOLDER = f"${{{_UNSET}}}"
_URL_NAME = re.compile(r"(url|endpoint)$")
_MARKERS = frozenset({"[]", "{}"})
_BASE: dict[str, Any] = {
    "github_org": "acme",
    "github_token": "ghp_fixturetoken",
    "human_reviewers": ["alice"],
    "symphonies": [{"name": "demo", "github_project_number": 100}],
}


@pytest.fixture(autouse=True)
def _variables(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(_UNSET, raising=False)
    monkeypatch.setenv(_EMPTY, "")


def _oracle() -> dict[str, set[tuple[str, ...]]]:
    """Paths found in pydantic's JSON schema: secrets (format password), URLs (format uri), strings, env dicts."""
    schema = TypeAdapter(ProjectConfiguration).json_schema(mode="validation")
    defs = schema.get("$defs", {})
    found: dict[str, set[tuple[str, ...]]] = {"secret": set(), "uri": set(), "string": set(), "env": set()}

    def visit(node: object, path: tuple[str, ...], refs: frozenset[str]) -> None:
        if not isinstance(node, dict):
            return
        if "$ref" in node:
            name = node["$ref"].rsplit("/", 1)[-1]
            if name not in refs:
                visit(defs[name], path, refs | {name})
            return
        if node.get("format") == "password":
            found["secret"].add(path)
        if node.get("format") == "uri":
            found["uri"].add(path)
        if node.get("type") == "string" and path:
            found["string"].add(path)
        extra = node.get("additionalProperties")
        if isinstance(extra, dict) and extra.get("type") == "string":
            found["env"].add((*path, "{}"))
        for name, child in node.get("properties", {}).items():
            visit(child, (*path, name), refs)
        for key in ("anyOf", "oneOf", "allOf"):
            for child in node.get(key, []):
                visit(child, path, refs)
        if "items" in node:
            visit(node["items"], (*path, "[]"), refs)
        if isinstance(extra, dict):
            visit(extra, (*path, "{}"), refs)

    visit(schema, (), frozenset())
    return found


_ORACLE = _oracle()


def _name(path: tuple[str, ...]) -> str:
    return next(segment for segment in reversed(path) if segment not in _MARKERS)


def _doc_for(path: tuple[str, ...], value: object) -> Any:
    if not path:
        return value
    segment, rest = path[0], path[1:]
    if segment == "[]":
        return [_doc_for(rest, value)]
    if segment == "{}":
        return {"NAME": _doc_for(rest, value)}
    return {segment: _doc_for(rest, value)}


def _ids(paths: list[tuple[str, ...]]) -> list[str]:
    return [".".join(p) for p in paths]


_URL_PATHS = sorted(
    p for p in (_ORACLE["string"] | _ORACLE["uri"]) if _URL_NAME.search(_name(p))
)
_ENV_PATHS = sorted(_ORACLE["env"])


# --- what the oracle says the model holds -------------------------------------------------------


def test_the_oracle_finds_the_same_seven_secrets_as_the_walk() -> None:
    assert sorted(_ORACLE["secret"]) == sorted(secret_paths(ProjectConfiguration))
    assert len(_ORACLE["secret"]) == 7


def test_the_env_dicts_are_the_performer_env_and_every_workflow_env() -> None:
    assert ("performer_endpoints", "[]", "env", "{}") in _ORACLE["env"]
    assert ("performers", "default", "workflow_env", "{}") in _ORACLE["env"]
    assert ("performers", "reviewer", "workflow_env", "{}") in _ORACLE["env"]


def test_the_url_named_fields_include_the_endpoint_and_host_urls_the_review_named() -> None:
    urls = {".".join(p) for p in _URL_PATHS}
    assert "endpoints.[].base_url" in urls
    assert "performer_endpoints.[].endpoint" in urls
    assert "model_endpoints.[].endpoint" in urls
    assert "github_api_url" in urls
    assert "dashboard_oidc.redirect_url" in urls


# --- URL and environment values are refused when a placeholder is unset ------------------------


@pytest.mark.parametrize("path", _URL_PATHS, ids=_ids(_URL_PATHS))
def test_every_url_named_field_refuses_an_unset_placeholder(path: tuple[str, ...]) -> None:
    problems = placeholder_problems(ProjectConfiguration, _doc_for(path, _PLACEHOLDER))
    assert problems, f"{'.'.join(path)} accepted an unset placeholder"
    assert _UNSET in problems[0][1]


@pytest.mark.parametrize("path", _ENV_PATHS, ids=_ids(_ENV_PATHS))
def test_every_environment_dict_value_refuses_an_unset_placeholder(path: tuple[str, ...]) -> None:
    problems = placeholder_problems(ProjectConfiguration, _doc_for(path, _PLACEHOLDER))
    assert problems, f"{'.'.join(path)} accepted an unset placeholder"
    assert _UNSET in problems[0][1]


def test_an_endpoint_base_url_carrying_an_unset_key_is_refused_at_save() -> None:
    raw = {**_BASE, "endpoints": [{"name": "e", "kind": "litellm", "base_url": f"https://h/v1?api_key={_PLACEHOLDER}"}]}
    errors = _validate_candidate(raw)
    assert errors
    assert errors[0].key == "endpoints[0].base_url"
    assert _UNSET in errors[0].message


def test_a_performer_env_value_carrying_an_unset_placeholder_is_refused_at_save() -> None:
    raw = {
        **_BASE,
        "performer_endpoints": [{
            "id": "remote", "mode": "persistent", "roles": ["implementer"],
            "endpoint": "http://performer.example:8080/", "image": "performer:latest",
            "env": {"API_KEY": _PLACEHOLDER},
        }],
    }
    errors = _validate_candidate(raw)
    assert errors
    assert errors[0].key == "performer_endpoints[0].env.API_KEY"
    assert _UNSET in errors[0].message


def test_a_full_url_in_another_string_field_refuses_an_unset_placeholder() -> None:
    problems = placeholder_problems(ProjectConfiguration, {"agent_host": f"https://host.example/{_PLACEHOLDER}"})
    assert [field for field, _ in problems] == ["agent_host"]


def test_a_host_without_a_scheme_in_another_string_field_is_not_a_url_and_is_not_refused() -> None:
    assert placeholder_problems(ProjectConfiguration, {"agent_host": f"host.example/{_PLACEHOLDER}"}) == []


def test_a_url_named_field_added_later_is_covered_without_a_list() -> None:
    class _Later(BaseModel):
        callback_url: str = ""
        gateway_endpoint: str | None = None

    problems = placeholder_problems(_Later, {"callback_url": _PLACEHOLDER, "gateway_endpoint": _PLACEHOLDER})
    assert [field for field, _ in problems] == ["callback_url", "gateway_endpoint"]


# --- the text fields that may legitimately hold ${...} are never refused -------------------------

def _is_text(path: tuple[str, ...]) -> bool:
    name = _name(path)
    return name in TEXT_FIELD_NAMES or any(name.endswith(suffix) for suffix in TEXT_FIELD_SUFFIXES)


_TEXT_PATHS = sorted(p for p in _ORACLE["string"] if _is_text(p))


def test_every_text_field_name_matches_a_real_string_field() -> None:
    names = {_name(p) for p in _ORACLE["string"]}
    for name in TEXT_FIELD_NAMES:
        assert name in names, name


@pytest.mark.parametrize("path", _TEXT_PATHS, ids=_ids(_TEXT_PATHS))
def test_a_text_field_with_an_unset_placeholder_is_not_refused(path: tuple[str, ...]) -> None:
    doc = _doc_for(path, f"https://example.test/{_PLACEHOLDER}")
    assert placeholder_problems(ProjectConfiguration, doc) == []


def test_the_text_fields_include_the_templates_prompts_and_shell_command() -> None:
    dotted = {".".join(p) for p in _TEXT_PATHS}
    assert "advocate.redirect_template" in dotted
    assert "agent_command" in dotted
    assert "notifications.channels.[].message_template" in dotted
    assert "personas.advocate.instructions" in dotted
    assert "personas.advocate.scope_behavior.full.prompt_addon" in dotted


# --- the boot refusal exits 1 with the message on stderr ----------------------------------------


def _daemon_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("COORDINARE_")}
    src = str(_REPO_ROOT / "src")
    env["PYTHONPATH"] = os.pathsep.join(
        [src, str(_REPO_ROOT / "agent/performer/src"), str(_REPO_ROOT / "packages/ci_detection/src"),
         str(_REPO_ROOT / "packages/service_inference/src"), str(_REPO_ROOT / "packages/routing_models/src")],
    )
    env.pop(_UNSET, None)
    env[_EMPTY] = ""
    return env


def _boot(tmp_path: Path, raw: dict[str, Any]) -> subprocess.CompletedProcess[str]:
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(raw, sort_keys=False))
    return subprocess.run(
        [sys.executable, "-m", "coordinare", "--config", str(path)],
        cwd=tmp_path, env=_daemon_env(), capture_output=True, text=True, timeout=120, check=False,
    )


def test_boot_with_an_unset_placeholder_exits_one_and_prints_the_message(tmp_path: Path) -> None:
    raw = {
        **_BASE,
        "notifications": {"channels": [{
            "name": "mail", "type": "email", "smtp_host": "smtp.example", "smtp_recipient": "a@example",
            "smtp_password": _PLACEHOLDER,
        }]},
    }
    result = _boot(tmp_path, raw)
    assert result.returncode == 1, result.stderr
    assert f"ERROR: notifications.channels[0].smtp_password still holds ${{{_UNSET}}}" in result.stderr


def test_boot_with_an_empty_variable_exits_one_and_names_it(tmp_path: Path) -> None:
    result = _boot(tmp_path, {**_BASE, "github_token": f"${{{_EMPTY}}}"})
    assert result.returncode == 1, result.stderr
    assert f"ERROR: github_token uses ${{{_EMPTY}}}" in result.stderr
    assert f"{_EMPTY} is set but empty" in result.stderr


# --- an empty variable gets its own message -----------------------------------------------------


def test_save_names_an_empty_variable_for_a_token_instead_of_requiring_one() -> None:
    errors = _validate_candidate({**_BASE, "github_token": f"${{{_EMPTY}}}"})
    assert errors
    assert errors[0].key == "github_token"
    assert f"{_EMPTY} is set but empty" in errors[0].message
    assert "requires" not in errors[0].message


def test_save_names_an_empty_variable_for_a_notification_secret() -> None:
    raw = {
        **_BASE,
        "notifications": {"channels": [{
            "name": "mail", "type": "email", "smtp_host": "smtp.example", "smtp_recipient": "a@example",
            "smtp_password": f"${{{_EMPTY}}}",
        }]},
    }
    errors = _validate_candidate(raw)
    assert errors
    assert f"{_EMPTY} is set but empty" in errors[0].message


def test_an_empty_variable_in_a_url_is_a_legitimately_blank_value_and_is_not_refused() -> None:
    assert placeholder_problems(ProjectConfiguration, _doc_for(("endpoints", "[]", "base_url"), f"https://h/v1?k=${{{_EMPTY}}}")) == []


# --- walk edge cases, checked by a different algorithm -----------------------------------------


class _OptionalAnnotated(BaseModel):
    a: Annotated[SecretStr, Field(description="nested in a union")] | None = None


class _TupleOfSecrets(BaseModel):
    b: tuple[SecretStr, ...] = ()


class _SetOfSecrets(BaseModel):
    c: set[SecretStr] = set()


class _NestedContainers(BaseModel):
    d: dict[str, list[SecretStr | None]] = {}


class _Node(BaseModel):
    secret: SecretStr | None = None
    children: list[_Node] = []


_Node.model_rebuild()


class _Tree(RootModel[dict[str, "_Tree"] | SecretStr]):
    pass


_Tree.model_rebuild()


def _instance_secret_paths(value: object, path: tuple[str, ...] = ()) -> set[tuple[str, ...]]:
    """Search an instance's values for SecretStr: a walk of the data, not of the annotations."""
    if isinstance(value, SecretStr):
        return {path}
    found: set[tuple[str, ...]] = set()
    if isinstance(value, RootModel):
        found |= _instance_secret_paths(value.root, path)
    elif isinstance(value, BaseModel):
        for name in type(value).model_fields:
            found |= _instance_secret_paths(getattr(value, name), (*path, name))
    elif isinstance(value, (list, tuple, set, frozenset)):
        for item in value:
            found |= _instance_secret_paths(item, (*path, "[]"))
    elif isinstance(value, dict):
        for item in value.values():
            found |= _instance_secret_paths(item, (*path, "{}"))
    return found


@pytest.mark.parametrize(
    ("model", "instance", "expected"),
    [
        (_OptionalAnnotated, _OptionalAnnotated(a=SecretStr("x")), {("a",)}),
        (_TupleOfSecrets, _TupleOfSecrets(b=(SecretStr("y"),)), {("b", "[]")}),
        (_SetOfSecrets, _SetOfSecrets(c={SecretStr("z")}), {("c", "[]")}),
        (_NestedContainers, _NestedContainers(d={"k": [SecretStr("w"), None]}), {("d", "{}", "[]")}),
        (_Tree, _Tree(SecretStr("r")), {()}),
    ],
    ids=["optional-annotated", "tuple", "set", "dict-of-list-of-optional", "recursive-root-model"],
)
def test_the_walk_finds_each_edge_case_and_the_data_agrees(
    model: type[BaseModel], instance: BaseModel, expected: set[tuple[str, ...]],
) -> None:
    assert set(secret_paths(model)) == expected
    assert _instance_secret_paths(instance) == expected


def test_a_recursive_model_terminates_and_keeps_the_secret_it_reaches_first() -> None:
    assert secret_paths(_Node) == (("secret",),)
    found = _instance_secret_paths(_Node(secret=SecretStr("s"), children=[_Node(secret=SecretStr("t"))]))
    assert set(secret_paths(_Node)) <= found


def _has_cycle(model: type, ancestors: tuple[type, ...] = ()) -> bool:
    """True when a model reaches itself through its own fields (a depth-first search with ancestors)."""
    if model in ancestors:
        return True
    children: list[type] = []

    def collect(annotation: object) -> None:
        args = getattr(annotation, "__args__", ())
        if isinstance(annotation, type) and issubclass(annotation, BaseModel):
            children.append(annotation)
        for arg in args:
            collect(arg)

    fields = [info.annotation for info in model.model_fields.values()]
    if issubclass(model, RootModel):
        fields = [model.model_fields["root"].annotation]
    for annotation in fields:
        collect(annotation)
    return any(_has_cycle(child, (*ancestors, model)) for child in children)


def test_the_config_model_has_no_recursive_model_so_the_walk_reaches_every_secret() -> None:
    assert not _has_cycle(ProjectConfiguration)


def test_the_instance_search_agrees_with_the_walk_on_a_populated_config() -> None:
    raw = {
        **_BASE,
        "dashboard_auth_token": "a-dashboard-token-with-more-than-thirty-two-characters",
        "notifications": {"channels": [{
            "name": "mail", "type": "email", "smtp_host": "smtp.example", "smtp_recipient": "a@example",
            "smtp_password": "pw",
        }]},
        "performer_endpoints": [{
            "id": "remote", "mode": "persistent", "roles": ["implementer"],
            "endpoint": "http://performer.example:8080/", "image": "performer:latest", "auth_token": "tok",
        }],
    }
    found = _instance_secret_paths(ProjectConfiguration.model_validate(raw))
    expected = {
        ("github_token",),
        ("dashboard_auth_token",),
        ("notifications", "channels", "[]", "smtp_password"),
        ("performer_endpoints", "[]", "auth_token"),
    }
    assert found == expected
    assert expected <= set(secret_paths(ProjectConfiguration))


# --- the save path keys a refusal by its field path --------------------------------------------


def test_a_nested_secret_refusal_keeps_its_field_path_as_the_key() -> None:
    raw = {
        **_BASE,
        "notifications": {"channels": [{
            "name": "mail", "type": "email", "smtp_host": "smtp.example", "smtp_recipient": "a@example",
            "smtp_password": _PLACEHOLDER,
        }]},
    }
    errors = _validate_candidate(raw)
    assert errors
    assert errors[0].key == "notifications.channels[0].smtp_password"


def test_a_missing_app_key_file_is_keyed_by_the_key_path_not_the_dashboard_token(tmp_path: Path) -> None:
    raw = {
        "github_org": "acme",
        "human_reviewers": ["alice"],
        "github_auth": "app",
        "github_app_id": 1,
        "github_installation_id": 2,
        "github_private_key_path": str(tmp_path / "missing.pem"),
        "symphonies": [{"name": "demo", "github_project_number": 100}],
    }
    errors = _validate_candidate(raw)
    assert errors
    assert errors[0].key == "github_private_key_path"


def test_a_dashboard_bind_refusal_is_keyed_by_the_dashboard_token() -> None:
    errors = _validate_candidate({**_BASE, "dashboard_host": "0.0.0.0"})
    assert errors
    assert errors[0].key == "dashboard_auth_token"


def test_the_start_check_error_carries_the_field_it_refused() -> None:
    config = ProjectConfiguration(**{**_BASE, "notifications": {"channels": [{
        "name": "mail", "type": "email", "smtp_host": "smtp.example", "smtp_recipient": "a@example",
        "smtp_password": _PLACEHOLDER,
    }]}})
    with pytest.raises(StartCheckError) as info:
        validate_start_time(config)
    assert info.value.field == "notifications.channels[0].smtp_password"


def test_a_symphony_override_refusal_keeps_the_symphony_path() -> None:
    raw = {**_BASE, "symphonies": [{"name": "demo", "github_project_number": 100, "overrides": {
        "notifications": {"channels": [{"name": "mail", "type": "email", "smtp_host": "smtp.example",
                                        "smtp_recipient": "a@example", "smtp_password": _PLACEHOLDER}]},
    }}]}
    errors = _validate_candidate(raw)
    assert errors
    assert errors[0].key == "symphonies[0].overrides.notifications.channels[0].smtp_password"


def test_the_raw_check_reads_the_flat_multi_symphony_shape() -> None:
    raw = {**_BASE, "symphonies": [{"name": "demo", "github_project_number": 100,
                                    "overrides": {"github_token": _PLACEHOLDER}}]}
    problems = raw_placeholder_problems(raw)
    assert [field for field, _ in problems] == ["symphonies[0].overrides.github_token"]
    CoordinareConfiguration(**coerce_multi_symphony_raw(raw))


def test_the_settings_page_shows_a_nested_error_it_cannot_place_in_the_general_message(tmp_path: Path) -> None:
    """A nested key matches no input, so the page must show the message rather than drop it (node)."""
    assert shutil.which("node") is not None, "the Settings page check runs under node"
    source = (_REPO_ROOT / "src/coordinare/dashboard/dashboard_static/config.js").read_text()

    def fn(name: str) -> str:
        start = source.index(f"function {name}(")
        depth = 0
        for index in range(source.index("{", start), len(source)):
            depth += {"{": 1, "}": -1}.get(source[index], 0)
            if depth == 0:
                return source[start:index + 1]
        raise AssertionError(name)

    script = tmp_path / "settings_check.js"
    script.write_text(
        "var flashed = [];\n"
        "function cfgFlash(el, msg) { flashed.push(msg); }\n"
        "function cfgClearErrors() {}\n"
        "function cfgConflict() {}\n"
        "function cfgConflictMessage() { return ''; }\n"
        "var _cfgPending = null, _cfgRemovedRoute = null;\n"
        + fn("cfgApplyErrors") + "\n" + fn("cfgHandleSave") + "\n"
        "var container = { querySelector: function() { return null; } };\n"
        "var body = { errors: [{ key: 'notifications.channels[0].smtp_password', code: 'validation', message: 'M1' },"
        " { key: 'dashboard_auth_token', code: 'validation', message: 'M2' }] };\n"
        "cfgHandleSave(422, body, container, null, 'global', null);\n"
        "console.log(JSON.stringify(flashed));\n",
    )
    result = subprocess.run(["node", str(script)], capture_output=True, text=True, timeout=60, check=False)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.strip().splitlines()[-1]) == ["M1; M2"]
