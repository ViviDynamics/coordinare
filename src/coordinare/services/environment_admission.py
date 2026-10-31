"""Shared bounded declaration admission; no execution or source selection policy."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from coordinare_service_inference.environment.models import MAX_DUPLICATE_PAIRS
from coordinare_service_inference.environment.scope_revision import MAX_SCOPE_REVISION_CHANGES

from coordinare.services.environment_attempt import AttemptBudget, EnvironmentDiscoveryError
from coordinare.services.environment_discovery_prompts import correction_instruction
from coordinare.services.environment_proposal_patch import MAX_PATH_DEPTH, PATCH_INSTRUCTION
from coordinare.services.model_output_cap import REPETITION_FINISH

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from pydantic import ValidationError

    from coordinare.services.environment_discovery import DiscoveryLimits

_ASCII_CONTROL_BOUND = 32


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _valid_path(path: Any) -> bool:
    return (
        isinstance(path, str) and bool(path) and not path.startswith("/")
        and "\\" not in path and all(part not in {"", ".", ".."} for part in path.split("/"))
        and not any(ord(char) < _ASCII_CONTROL_BOUND for char in path)
    )


class _DuplicateJsonKeyError(ValueError):
    """Ambiguous model output must never select either duplicate value."""


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            message = "duplicate JSON key"
            raise _DuplicateJsonKeyError(message)
        result[key] = value
    return result


def _decode_reply(raw: str, name: str, limits: DiscoveryLimits) -> dict[str, Any]:
    if not isinstance(raw, str) or len(raw.encode("utf-8")) > limits.max_output_bytes:
        message = f"{name} output limit exceeded"
        raise EnvironmentDiscoveryError(message)
    try:
        reply = json.loads(raw, object_pairs_hook=_object)
    except _DuplicateJsonKeyError:
        message = f"{name} returned duplicate JSON keys"
        raise EnvironmentDiscoveryError(message, code="duplicate_json_key") from None
    except (ValueError, RecursionError):
        message = f"{name} returned malformed JSON"
        raise EnvironmentDiscoveryError(message, code="invalid_json_envelope") from None
    if not isinstance(reply, dict):
        message = f"{name} returned an invalid envelope"
        raise EnvironmentDiscoveryError(message, code="invalid_json_envelope")
    return reply


# The unknown key itself is never echoed (see _validation_feedback); the guidance says what to do with it.
UNKNOWN_KEY_GUIDANCE = (
    'The key shown as * at this location is not defined by the response schema. Remove it rather than renaming it: '
    'send only the keys the schema defines, with no notes, counts, placeholders or extra copies of a section beside '
    'them. Put any explanation in the rationale field, and send each section once under its schema key.')
SOURCE_DISPOSITION_GUIDANCE = (
    'Each source review decision needs exactly one disposition: covered, source_correction, source_gap or duplicate, '
    'with that disposition\'s required fields. Do not supply a status field; the daemon derives it. '
    'Use criterion_ids only for covered; affected_ids, gap_kind and inspection_requests only for source_gap; '
    'duplicate_of only for duplicate. Every other decision field must be null or []. '
    'Preserve every other decision while correcting this one.')


REVISION_LIMIT_GUIDANCE = (
    f'A scope revision may contain at most {MAX_SCOPE_REVISION_CHANGES} combined add and reassess operations. Established criteria are retained '
    'without resubmission: add only genuinely new obligations and reassess only retained unknown or not_applicable '
    'criteria. Submit the most important changes now; further obligations can follow in a later revision.')
LENGTH_GUIDANCE = ("Shorten this field to at most max_length (characters, or list items for a list): keep it a "
    "summary and leave the rest of the reply unchanged.")
REPLY_LENGTH_GUIDANCE = ("Your reply was cut off at the output limit before the envelope was complete. Reason briefly, "
    "keep rationales short, and return the complete envelope.")
REPETITION_GUIDANCE = ("Your reply was stopped because it began repeating the same text. Reason briefly, write each "
    "record once, and return the complete envelope.")
MAPPING_GUIDANCE = ("criterion_checks must map exactly every required scope criterion id: add each in missing_criteria; "
    "for each in invalid_criteria, map only existing required checks with no scope; drop keys that are not required "
    "criteria (unexpected_keys counts them).")
SCOPE_ID_COLLISION_GUIDANCE = ("A requirement, install, check or service id reuses a scope criterion id (S followed by 16 hex "
    "characters). Give each its own descriptive id, such as req-ruby or check-bundle; scope criterion ids appear only "
    "as criterion_checks keys. When unavailable lists them, rename exactly those records and leave every other record "
    "and id unchanged.")
GLOBAL_PART_GUIDANCE = ("This is the global part of a divided manifest. Leave criterion_checks empty: check authors map "
    "criteria. Checks with ids of c, two or three digits, optional letters and a hyphen (c00-, c00a-) belong to check "
    "authors, who write them in their own parts: leave every one of them out of the global part, and never keep a copy "
    "under another id. Rename only a global check of your own whose id happens to take that form.")
CREDENTIAL_GUIDANCE = (
    'Use ${NAME} for every operator credential and local credential. For a credential environment variable named K, '
    'its value must be exactly ${K}. '
    'A credential for a service this manifest starts, such as a local test database password, belongs in '
    'local_credentials as {name, service, purpose} and is referenced as ${NAME}; the trusted runner generates a fresh '
    'value every run, so data that stores it must live under {scratch}, and neither install commands nor activation can use it. '
    'An operator credential (an external service token or key) comes from the trusted runtime; do not invent one, and '
    'unavailable required values hold readiness. A secret no started service owns and no operator supplies, such as a '
    'framework secret key a test run needs, is generated inside the shell command that needs it and referred to there '
    'as $NAME, never ${NAME}, for example NAME=$(openssl rand -hex 64) bundle exec rspec or '
    'NAME=$(openssl rand -hex 64); export NAME; bin/test, never in a command env field. '
    'Never move a literal credential into argv, activation, scope text or another field; describe a credential-bearing '
    'URL from source without its password.')
LOCAL_CREDENTIAL_GUIDANCE = {
    'local_credential_invalid': ('Each local credential needs a unique name and the id of a service declared in this manifest. '
                                 'A secret no started service owns (for example a framework secret key) is not a local '
                                 'credential: generate it inside the command that needs it, for example '
                                 'NAME=$(openssl rand -hex 64) before the test command.'),
    'local_credential_unused': 'Reference every declared local credential as ${NAME}, or remove the declaration.',
    'local_credential_cache_data': ('A command that uses a generated credential must keep its data under {scratch} '
                                    'and must not reference {cache} in argv, cwd or env other than PATH; reach cached '
                                    'tools through activation path entries.'),
    'local_credential_in_install': ('Install commands and activation cannot use generated credentials; reference the '
                                    'credential only in the service and check commands that need it, or generate a '
                                    'secret no started service owns inside the command, for example '
                                    'NAME=$(openssl rand -hex 64).'),
}
# Phase rules a dependency edge broke; each names the command and the ids it may not depend on.
GRAPH_GUIDANCE = {
    'clean_dependency_install_only': ('Checks and service commands run in a clean consumer without install steps; remove '
                                      'the install ids from depends_on and rely on activation instead.'),
    'missing_prerequisite': 'Every depends_on id must be the id of a command declared in this manifest.',
    'cyclic_prerequisites': 'depends_on edges must not form a cycle; remove one edge between the named commands.',
    'install_dependency_phase': 'Install commands may depend only on other install commands.',
    'check_dependency_phase': ('Checks may depend only on other checks or on service init, start and health commands; '
                               'never on install commands or service stop.'),
    'service_dependency_phase': ('Service init, start and health may depend only on init, start or health commands of '
                                 'other services; never on checks or install commands. Their own order is implied. '
                                 'Remove the named ids from depends_on; if a precondition must be verified, do it '
                                 'inside the service command or in a check that depends on the service.'),
    'duplicate_service_ids': 'Each service needs a unique id.',
    'runtime_env_reserved': ('The private service runtime owns the listed environment variables; remove them from the '
                             'named command or activation env. Reach Docker through the runtime it provides.'),
    'duplicate_command_ids': 'Every install, check and service command needs its own id; rename the listed duplicates.',
    'duplicate_command_content': ('Each listed pair runs the same argv, cwd, env and scope under two ids. Keep one of the '
                                  'two (keep the required one if only one is required) and give it the union of both '
                                  'depends_on lists, the larger timeout_seconds, and required if either was required. Remap '
                                  "the dropped id's criteria to the kept id and rewrite depends_on entries that named the "
                                  'dropped id. A command that needs different behaviour must differ in argv, cwd, env or scope.'),
    'duplicate_requirement_ids': 'Every requirement needs its own id; rename the listed duplicates.',
    'service_scope_mismatch': ("All of a service's init, start, health and stop commands share the start command's "
                               'scope; set the listed commands to it.'),
    'scope_required_service': ('A required service keeps every lifecycle command required; mark the listed commands '
                               'required, or make the whole service optional if the scope allows.'),
    'required_check_missing': 'At least one check must be required.',
    'requirement_source_uninspected': ("A requirement's source must be a file you inspected; read it, or cite one you "
                                       'read.'),
    'empty_reason_missing': 'With no requirements, explain in empty_reason why the environment needs none.',
    'shell_local_template_reference': ('The named command assigns the listed variables itself, so reference them as $NAME '
                                       '(no braces): ${NAME} is a template the runner fills from its environment '
                                       'before the command runs, and it refuses names it does not have.'),
    'record_id_collision': ('Each listed id is used by more than one record. Rename the requirement that shares it '
                            '(for example req-<id>) and keep the check, install and service command ids unchanged.'),
    'global_obligation_removed': ('Your earlier proposals declared the listed required records; keep each id and revise '
                                  'the record in place. Only an id that reuses a scope criterion id may be renamed.'),
    'connectivity_gate': ('The named command depends on an install that only tests reachability. Remove the listed ids '
                          'from its depends_on: a command that needs the network fails on its own, and a gate only '
                          'stops every install that depends on it.'),
    'foreign_architecture_artifact': ('The named command downloads a release artifact built for another CPU architecture '
                                      'than the install platform in execution_capabilities.platform. Download the artifact '
                                      'for that platform instead; request a foreign platform only when the repository '
                                      'explicitly requires one.'),
    'system_package_install': ('The named install runs a system package manager (apt-get or apt install, update or '
                               'download, dpkg -i, apk add, dnf or yum install), which this carrier refuses: it has no '
                               'Linux capabilities, and even apt update and apt download need them. Download a prebuilt '
                               'release archive for execution_capabilities.platform, or fetch the .deb files you need '
                               'from a package mirror with curl and unpack them under {cache} with dpkg-deb -x, and point '
                               'activation at them.'),
    'self_matching_process_pattern': ('The named command runs pkill -f or pgrep -f inside a shell script that does more than '
                                      'that, so the pattern matches the script\'s own shell and kills or finds it. Write the '
                                      'process id to a file under {scratch} when it starts, and stop it by killing that '
                                      'pid.'),
    'masked_check_status': ('The named required check discards the failure of the work it verifies: it ends with a '
                            'command masked by || true, || :, || echo or || exit 0, or masks a chain of commands '
                            'joined by && that way, so it passes whatever that work does. Let the verified command\'s '
                            'own exit status be the check\'s: drop the fallback, or move preparation that may fail '
                            'harmlessly into its own step and keep the verified commands unmasked.'),
    'activation_path_replaced': ('activation.env sets PATH, which replaces the guest\'s own PATH for every install, service '
                                 'and check command, so even the shell and system tools are not found. Remove PATH from '
                                 'activation.env and list each tool directory in activation.path instead: those entries '
                                 'are prepended to the guest PATH.'),
    'activation_path_uncreated': ('Each listed activation path entry (its 0-based position in activation.path) is not '
                                  'created or populated by any install command. Have an install command create that '
                                  'path or a directory it lives in, or remove the entry.'),
}


def nested_responses_guidance(key: str | None) -> str:
    """Names the envelope's own main key, which the misplaced finding_responses was nested under."""
    if key is None:
        return ('finding_responses belongs at the top level of the envelope, beside its main key, never nested inside '
                'it: return {"<main key>": {...}, "finding_responses": [...]}.')
    return (f'finding_responses belongs beside {key} at the top level of the envelope, never inside {key}: '
            f'return {{"{key}": {{...}}, "finding_responses": [...]}}.')


NESTED_RESPONSES_GUIDANCE = nested_responses_guidance('scope')
SOURCE_PATH_GUIDANCE = (
    'Each source must be one exact repository-relative file path that you inspected: no directories, ./ prefixes, '
    'trailing slashes, URLs or annotations.')


def _revision_limit(error: Mapping[str, Any], location: list[Any]) -> dict[str, Any] | None:
    """Only daemon-owned counts escape; the oversized proposal content is never echoed."""
    context = error.get('ctx') or {}
    if error['type'] == 'too_long' and 'scope_revision' in location and location[-1:] in (['add'], ['reassess']):
        return {'limit': context.get('max_length'), 'submitted': context.get('actual_length'), 'guidance': REVISION_LIMIT_GUIDANCE}
    if error['type'] == 'scope_revision_limit':
        return {'limit': context.get('limit'), 'submitted': context.get('submitted'), 'guidance': REVISION_LIMIT_GUIDANCE}
    return None


NUMERIC_BOUND_CODES = frozenset({"greater_than", "greater_than_equal", "less_than", "less_than_equal"})
# The schema-owned value a choice or pattern error names: (context key, reported name).
CHOICE_CONTEXT = {"literal_error": ("expected", "allowed"), "union_tag_not_found": ("expected_tags", "allowed"),
                  "union_tag_invalid": ("expected_tags", "allowed"), "string_pattern_mismatch": ("pattern", "pattern")}


def _schema_bounds(code: str, error: Mapping[str, Any], *, guided: bool) -> dict[str, Any]:
    """The schema's own bound for a size, range, choice or missing-field error; submitted values are never echoed."""
    context = error.get("ctx") or {}
    location = error.get("loc") or ()
    found: dict[str, Any] = {}
    if code == "missing" and location and isinstance(location[-1], str):
        # A missing field is always a declared schema field, so naming it echoes nothing the model wrote.
        found["missing_field"] = location[-1][:64]
    if code in {"string_too_long", "too_long"} and isinstance(context.get("max_length"), int):
        found.update({"max_length": context["max_length"], **({} if guided else {"guidance": LENGTH_GUIDANCE})})
    if code in {"string_too_short", "too_short"} and isinstance(context.get("min_length"), int):
        found["min_length"] = context["min_length"]
    if code in NUMERIC_BOUND_CODES:
        found.update({name: context[key] for key, name in (("gt", "greater_than"), ("ge", "at_least"),
                                                           ("lt", "less_than"), ("le", "at_most"))
                      if isinstance(context.get(key), int | float)})
    if code == "scope_required_check_mapping":
        found.update({key: context[key] for key in ("missing_criteria", "invalid_criteria", "unexpected_keys") if key in context})
        found["guidance"] = MAPPING_GUIDANCE
    if code in CHOICE_CONTEXT and isinstance(context.get(CHOICE_CONTEXT[code][0]), str):
        found[CHOICE_CONTEXT[code][1]] = context[CHOICE_CONTEXT[code][0]][:512]
    return found


GRAPH_ID = re.compile(r"[A-Za-z0-9_.:-]{1,128}")


def _graph_ids(error: Mapping[str, Any]) -> dict[str, Any]:
    """Command ids already passed the schema's id pattern; anything else is dropped, never echoed."""
    context = error.get("ctx") or {}
    found: dict[str, Any] = {}
    if isinstance(context.get("command_id"), str) and GRAPH_ID.fullmatch(context["command_id"]):
        found["command_id"] = context["command_id"]
    unavailable = [value for value in context.get("unavailable") or [] if isinstance(value, str) and GRAPH_ID.fullmatch(value)]
    if unavailable:
        found["unavailable"] = unavailable[:8]
    return found


PAIR_SIZE = len(('first', 'second'))


def _id_pairs(error: Mapping[str, Any]) -> list[list[str]]:
    """Each pair names two schema-validated command ids; a malformed pair is dropped, never echoed."""
    pairs = (error.get('ctx') or {}).get('pairs') or []
    return [pair for pair in pairs if isinstance(pair, list) and len(pair) == PAIR_SIZE
            and all(isinstance(value, str) and GRAPH_ID.fullmatch(value) for value in pair)][:MAX_DUPLICATE_PAIRS]


def _validation_feedback(exc: ValidationError) -> list[dict[str, Any]]:
    """Do not expose arbitrary keys, candidate values, validator context or prose."""
    allowed_fields = {"requirements", "checks", "install", "services", "activation", "id", "source", "description",
                      "depends_on", "required", "scope", "argv", "cwd", "env", "path", "timeout_seconds",
                      "schema_version", "policy_version", "empty_reason", "inputs", "manifest_digest",
                      "source_commit", "symphony_name", "sources", "verdict", "rationale", "capabilities_digest", "inventory_digest",
                      "commands", "citations", "questions", "probe", "manifest", "assessment", "findings", "decisions",
                      "finding_responses", "finding_id", "issue", "category", "affected_ids", "duplicate_of", "status",
                      "capability_unavailable", "criteria", "applicability", "usability", "runtime_needs", "scope_assessment", "criterion_checks", "runtime_endpoints", "scope_revision", "base_digest", "add", "reassess", "criterion_id", "criterion",
                      "disposition", "criterion_ids", "gap_kind", "inspection_requests", "covered", "source_correction",
                      "source_gap", "duplicate", "local_credentials", "name", "purpose", "service",
                      # Envelope keys, so misplaced finding_responses guidance can name the real one.
                      "shard_summary", "listing_triage", "scope_consolidation", "shard_checks", "verdicts", "groups"}
    result = []
    # The key a misplaced finding_responses was nested under is the envelope's main key, named in the guidance.
    nested_key = next((str(error["loc"][0]) for error in exc.errors(include_url=False, include_input=False)
                       if error["type"] == "extra_forbidden" and tuple(error["loc"][1:2]) == ("finding_responses",)
                       and error["loc"][0] in allowed_fields), None)
    for error in exc.errors(include_url=False, include_input=False)[:16]:
        location = [part if (isinstance(part, str) and part in allowed_fields) or type(part) is int else "*"
                    for part in error["loc"][:8]]
        kind = error["type"]
        code = kind if kind in {
            "missing", "extra_forbidden", "string_type", "list_type", "dict_type", "bool_type",
            "literal_error", "int_type", "string_pattern_mismatch", "string_too_short", "string_too_long", "too_short", "too_long",
            "requirement_check_reference", "runtime_credential_reference",
            "probe_id_maps", "probe_cited_question", "probe_required_true", "probe_citations_uninspected",
            "scope_identity_mismatch", "scope_required_check_mapping", "scope_required_service", "scope_id_collision",
            "finding_citations_invalid", "union_tag_not_found", "union_tag_invalid", "global_part_invalid",
            "greater_than", "greater_than_equal", "less_than", "less_than_equal", "int_parsing", "float_parsing",
            "bool_parsing", "int_from_float", *LOCAL_CREDENTIAL_GUIDANCE, *GRAPH_GUIDANCE,
        } else "invalid_contract_value"
        scope_locations = {'scope_required_check_mapping': 'criterion_checks', 'scope_identity_mismatch': 'scope_contract_digest',
                           'scope_required_service': 'services', 'scope_id_collision': 'id'}
        if code in scope_locations:
            location = [scope_locations[code]]
        detail: dict[str, Any] = {"location": location, "code": code, **(_revision_limit(error, location) or {})}
        if code == "invalid_contract_value" and "sources" in location and "guidance" not in detail:
            detail["guidance"] = SOURCE_PATH_GUIDANCE
        if code == "finding_citations_invalid" or (code == "too_short" and location[-1:] == ["sources"]):
            detail["guidance"] = ("Supply a nonempty list of unique exact inspected file paths as source citations. "
                "Even an open finding response needs supporting source evidence. "
                "Do not use directory paths, duplicate citations or uninspected files; inspect relevant source first.")
        if code in {"union_tag_not_found", "union_tag_invalid"} or (
                code == "extra_forbidden" and "scope_assessment" in location and "decisions" in location):
            detail["guidance"] = SOURCE_DISPOSITION_GUIDANCE
        unknown_key = code == "extra_forbidden" and location[-1:] == ["*"] and "guidance" not in detail
        if code == "global_part_invalid" or unknown_key:
            detail["guidance"] = GLOBAL_PART_GUIDANCE if code == "global_part_invalid" else UNKNOWN_KEY_GUIDANCE
        if code == "scope_id_collision":
            detail.update(_graph_ids(error), guidance=SCOPE_ID_COLLISION_GUIDANCE)
        detail.update(_schema_bounds(code, error, guided="guidance" in detail))
        if code == "runtime_credential_reference":
            detail["guidance"] = CREDENTIAL_GUIDANCE
        if code in LOCAL_CREDENTIAL_GUIDANCE:
            detail.update(_graph_ids(error), guidance=LOCAL_CREDENTIAL_GUIDANCE[code])
        if code in GRAPH_GUIDANCE:
            detail.update(_graph_ids(error), guidance=GRAPH_GUIDANCE[code])
        if code == 'duplicate_command_content':
            detail['pairs'] = _id_pairs(error)
        if (code == "extra_forbidden" and location[1:2] == ["finding_responses"] and location[0] != "*") or (
                code == "missing" and location == ["finding_responses"]):
            detail["guidance"] = nested_responses_guidance(nested_key)
        result.append(detail)
    return result


@dataclass(frozen=True)
class CorrectionContext:
    """The requesting conversation owns its reply format and patch permission."""

    envelope: str
    can_patch: bool = False
    finish_reason: str | None = None


def _repair(messages: list[dict[str, str]], raw: str, errors: list[dict[str, Any]], budget: AttemptBudget, *,
            context: CorrectionContext | None = None) -> None:
    explain_rejection(messages, raw, errors, budget, context=context)
    budget.reserve_repair("schema")


def explain_rejection(messages: list[dict[str, str]], raw: str, errors: list[dict[str, Any]], budget: AttemptBudget, *,
                      context: CorrectionContext | None = None) -> None:
    """Return a rejected reply with its errors; callers own how the correction is counted."""
    # Parallel conversations share counters, but each owns the format requested for its reply.
    context = context or CorrectionContext(budget.expected_envelope, finish_reason=budget.last_finish_reason)
    envelope, can_patch = context.envelope, context.can_patch
    if context.finish_reason == 'length':
        # The reply was cut off at the output limit, usually by long hidden reasoning, not by the envelope itself.
        errors = [*errors, {'code': 'reply_length_limit', 'location': [],
                            'guidance': REPLY_LENGTH_GUIDANCE}]
    if context.finish_reason == REPETITION_FINISH:
        errors = [*errors, {'code': 'reply_repetition', 'location': [], 'guidance': REPETITION_GUIDANCE}]
    if budget.scope_contract is not None:
        required_ids = [c.id for c in budget.scope_contract.definition.criteria if c.applicability == 'required']
        for error in errors:
            if error['code'] == 'scope_required_check_mapping':
                error['required_criterion_ids'] = required_ids
    if envelope == 'scope_assessment' and budget.scope_findings is not None:
        for error in errors:
            if error['code'] == 'review_findings_invalid' and error['location'] == ['scope_assessment', 'decisions']:
                error.setdefault('required_decision_ids', [record.id for record in budget.scope_findings.records])
    if can_patch and envelope in {'manifest', 'assessment'}:
        for error in errors:
            location = error['location']
            if location and '*' not in location:
                root = envelope
                path = location if location[0] in {'manifest', 'finding_responses', 'assessment'} else [root, *location]
                if len(path) <= MAX_PATH_DEPTH:
                    error['correction_path'] = path
    budget.validation_codes.extend(error["code"] for error in errors)
    messages.extend([{"role": "assistant", "content": raw},
                     {"role": "user", "content": _json({"validation_errors": errors,
                        "expected_envelope": envelope,
                        "instruction": (PATCH_INSTRUCTION if can_patch else correction_instruction(envelope)) +
                        "When supplied, copy correction_path exactly into the patch path; location is a diagnostic field path. "
                        "Preserve every required obligation and inspected input. "
                        "Never rename or remove existing required requirement/check/service IDs; revise their definitions in place. "
                        "Use list_files for directories and read_files only for exact file paths from listings. " +
                        ("Use required executable check IDs, not commands or install-only prerequisites. "
                         if envelope in {"manifest", "manifest_delta", "assessment", "shard_checks"} else
                         "Preserve source-defined scope; do not generate commands during source review. "
                         if envelope in {"scope", "scope_revision", "scope_assessment", "shard_summary",
                                                         "listing_triage", "scope_consolidation"} else "") +
                        "Independent review and successful clean execution remain mandatory."})}])


def _checked_inventory(paths: Sequence[str], bounds: DiscoveryLimits) -> list[str]:
    if isinstance(paths, str) or len(paths) > bounds.max_inventory_entries:
        message = "source inventory limit exceeded or invalid inventory"
        raise EnvironmentDiscoveryError(message)
    if any(not _valid_path(path) for path in paths) or len(set(paths)) != len(paths):
        message = "source inventory contains invalid or duplicate paths"
        raise EnvironmentDiscoveryError(message)
    inventory = sorted(paths)
    if len(_json(inventory).encode()) > bounds.max_source_bytes:
        message = "source inventory byte limit exceeded"
        raise EnvironmentDiscoveryError(message)
    return inventory
