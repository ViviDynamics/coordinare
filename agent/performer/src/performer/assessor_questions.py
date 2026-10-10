"""Preserve explicit questions in a malformed assessment without assuming readiness."""
from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING

from performer.models import _redact_secrets

if TYPE_CHECKING:
    from collections.abc import Callable

_JSON_FIELD = re.compile(r'(?P<key>"(?:[^"\\]|\\.)*")\s*:\s*')
_ASSESSMENT_KEYS = frozenset({"questions", "sufficient", "assessment", "ready"})


def _container_end(raw: str, start: int) -> int:
    """Bound a metadata value, including one truncated before its closing bracket."""
    containers: list[str] = []
    in_string = escaped = False
    for index in range(start, len(raw)):
        char = raw[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
        elif char == '"':
            in_string = True
        elif char in "{[":
            containers.append(char)
        elif char in "}]" and containers and containers[-1] == {"}": "{", "]": "["}[char]:
            containers.pop()
            if not containers:
                return index + 1
    return len(raw)


def _question_value_objects(raw: str, start: int) -> list[tuple[int, int]]:
    """Question arrays contain strings, never nested assessment objects.

    Read complete elements even when the outer array is truncated. Stop at
    malformed array syntax so a later complete duplicate field can still be
    recovered rather than being swallowed by the damaged first array.
    """
    ranges: list[tuple[int, int]] = []
    index = start + 1
    decoder = json.JSONDecoder()
    while index < len(raw):
        while index < len(raw) and raw[index].isspace():
            index += 1
        try:
            value, end = decoder.raw_decode(raw, index)
        except ValueError:
            if raw[index:index + 1] in ("{", "["):
                ranges.append((index, _container_end(raw, index)))
            break
        if isinstance(value, (dict, list)):
            ranges.append((index, end))
        index = end
        while index < len(raw) and raw[index].isspace():
            index += 1
        if raw[index:index + 1] != ",":
            break
        index += 1
    return ranges


def _keyed_value_ranges(raw: str) -> list[tuple[int, int]]:
    """Protect key tokens and keyed containers/strings from root-array scans."""
    keyed_containers = [field.span("key") for field in _JSON_FIELD.finditer(raw)]
    keyed_containers.extend(
        (field.end(), _container_end(raw, field.end()))
        for field in _JSON_FIELD.finditer(raw)
        if raw[field.end():field.end() + 1] in ("{", "[")
    )
    decoder = json.JSONDecoder()
    for field in _JSON_FIELD.finditer(raw):
        start = field.end()
        if raw[start:start + 1] != '"':
            continue
        try:
            _, end = decoder.raw_decode(raw, start)
        except ValueError:
            continue
        keyed_containers.append((start, end))
    return keyed_containers



def _leading_string_ranges(raw: str) -> list[tuple[int, int]]:
    """Keep a complete scalar prefix atomic when followed by a JSON separator."""
    start = len(raw) - len(raw.lstrip())
    if raw[start:start + 1] != '"':
        return []
    try:
        _, end = json.JSONDecoder().raw_decode(raw, start)
    except ValueError:
        return []
    boundary = end
    while boundary < len(raw) and raw[boundary].isspace():
        boundary += 1
    if boundary == len(raw) or raw[boundary] in ",]}":
        return [(start, end)]
    return []


def _root_array_ranges(raw: str) -> list[tuple[int, int]]:
    """Bind array child containers without letting prose swallow root fields."""
    keyed_containers = [*_keyed_value_ranges(raw), *_leading_string_ranges(raw)]
    ranges: list[tuple[int, int]] = []
    decoder = json.JSONDecoder()
    for index, char in enumerate(raw):
        if char == "[" and not any(
            start <= index < end for start, end in [*keyed_containers, *ranges]
        ):
            limit = _container_end(raw, index)
            cursor = index + 1
            while cursor < limit:
                owned = next((end for start, end in keyed_containers if start <= cursor < end), None)
                if owned is not None:
                    cursor = owned
                    continue
                try:
                    value, end = decoder.raw_decode(raw[:limit], cursor)
                except ValueError:
                    if raw[cursor] in "{[":
                        end = _container_end(raw, cursor)
                        ranges.append((cursor, end))
                        cursor = end
                    else:
                        # Root arrays fail closed across malformed elements
                        # and separators. Valid strings remain atomic.
                        cursor += 1
                else:
                    if isinstance(value, (dict, list)):
                        ranges.append((cursor, end))
                    elif isinstance(value, str):
                        keyed_containers.append((cursor, end))
                    cursor = end
    return ranges


def _selected_fence_span(raw: str, extract_json: Callable[..., object]) -> tuple[int, int] | None:
    """Use the successful canonical candidate, not a second code-fence parser."""
    span = None

    def selected(kind: str, start: int, end: int) -> None:
        nonlocal span
        if kind == "fence":
            span = (start, end)

    extract_json(raw, candidate_callback=selected)
    return span


def _assessment_fields(
    raw: str, *, exclude_metadata: bool = True,
    fence_span: tuple[int, int] | None = None,
) -> list[tuple[str, int, int]]:
    if fence_span is not None:
        # Unclosed prose containers cannot own a successfully parsed fence.
        # Preserve explicit root questions outside it as separate fragments.
        start, end = fence_span
        return [
            (key, offset + field_start, offset + field_end)
            for offset, segment in ((0, raw[:start]), (start, raw[start:end]), (end, raw[end:]))
            for key, field_start, field_end in _assessment_fields(segment, exclude_metadata=exclude_metadata)
        ]
    fields = []
    metadata_ranges: list[tuple[int, int]] = []
    assessment_ranges: list[tuple[int, int]] = []
    root_arrays = _root_array_ranges(raw) if exclude_metadata else []
    for field in _JSON_FIELD.finditer(raw):
        try:
            key = json.loads(field.group("key"))
        except ValueError:
            continue
        if any(start <= field.start() < end for start, end in root_arrays):
            if key in _ASSESSMENT_KEYS:
                # Preserve the invalid-contract signal without recovering
                # questions owned by an array element.
                fields.append(("invalid_root_array", field.start(), field.end()))
            continue
        if exclude_metadata and any(start <= field.start() < end for start, end in metadata_ranges):
            continue
        if key in _ASSESSMENT_KEYS:
            fields.append((key, field.start(), field.end()))
        value_start = field.end()
        first = raw[value_start:value_start + 1]
        immediate_assessment = key == "assessment" and first == "{" and not any(
            start <= field.start() < end for start, end in assessment_ranges
        )
        if key == "questions" and first == "[":
            metadata_ranges.extend(_question_value_objects(raw, value_start))
        elif immediate_assessment:
            assessment_ranges.append((value_start, _container_end(raw, value_start)))
        elif first in ("{", "["):
            metadata_ranges.append((value_start, _container_end(raw, value_start)))
    return fields


def assessment_fields_outside_object(raw: str, extract_json: Callable[..., object]) -> bool:
    """A nested object is not an assessment when its contract fields lie outside it."""
    fence_span = _selected_fence_span(raw, extract_json)
    start, end = fence_span if fence_span is not None else (raw.find("{"), raw.rfind("}"))
    fields = _assessment_fields(raw, fence_span=fence_span)
    if any(key == "invalid_root_array" for key, _, _ in fields):
        return True
    if not fields and _assessment_fields(raw, exclude_metadata=False):
        return True
    return any(
        start < 0 or field_start < start or field_end > end
        for _, field_start, field_end in fields
    )


class _ObjectPairs(list[tuple[str, object]]):
    """Keep object fields distinct from arrays and retain duplicate keys."""


def _assessment_pairs(raw: str, extract_json: Callable[..., object]) -> _ObjectPairs | None:
    value = extract_json(raw, object_pairs_hook=_ObjectPairs)
    return value if isinstance(value, _ObjectPairs) else None


def assessment_has_invalid_field_types(output: dict) -> bool:
    """Malformed controls and question values must never use truthiness/coercion."""
    objects = [(output, "sufficient")]
    if isinstance(output.get("assessment"), dict):
        verdict = output["assessment"].get("verdict", "work")
        if not isinstance(verdict, str) or verdict not in ("work", "not_work", "needs_split"):
            return True
        objects.append((output["assessment"], "ready"))
    for obj, control in objects:
        if control in obj and not isinstance(obj[control], bool):
            return True
        if "questions" in obj and (
            not isinstance(obj["questions"], list)
            or any(not isinstance(q, str) for q in obj["questions"])
        ):
            return True
    return False


def _contract_objects(pairs: _ObjectPairs) -> list[_ObjectPairs]:
    return [pairs, *[
        value for key, value in pairs
        if key == "assessment" and isinstance(value, _ObjectPairs)
    ]]


def assessment_has_duplicate_contract_fields(raw: str, extract_json: Callable[..., object]) -> bool:
    """Reject ambiguous assessment fields while ignoring unrelated metadata."""
    pairs = _assessment_pairs(raw, extract_json)
    if pairs is None:
        return False
    for index, obj in enumerate(_contract_objects(pairs)):
        relevant = _ASSESSMENT_KEYS if index == 0 else {"ready", "questions", "verdict"}
        keys = [key for key, _ in obj if key in relevant]
        if len(keys) != len(set(keys)):
            return True
    return False


def _question_candidates(raw: str, fields: list[tuple[str, int, int]], extract_json: Callable[..., object]) -> list[object]:
    if not any(key == "questions" for key, _, _ in fields):
        return []
    pairs = _assessment_pairs(raw, extract_json)
    if pairs is not None and not assessment_fields_outside_object(raw, extract_json):
        return [value for obj in _contract_objects(pairs) for key, value in obj if key == "questions"]
    candidates = []
    for key, _, end in fields:
        if key == "questions":
            try:
                value, _ = json.JSONDecoder().raw_decode(raw[end:])
                candidates.append(value)
            except ValueError:
                continue
    return candidates


def assessment_fragment_questions(raw: str, extract_json: Callable[..., object]) -> tuple[bool, list[str]]:
    """Identify contract fragments; recover only complete arrays of question strings."""
    fields = _assessment_fields(raw, fence_span=_selected_fence_span(raw, extract_json))
    questions: list[str] = []
    for value in _question_candidates(raw, fields, extract_json):
        if not isinstance(value, list) or isinstance(value, _ObjectPairs) or any(not isinstance(q, str) for q in value):
            continue
        for question in value:
            safe_question = _redact_secrets(question)
            if safe_question.strip() and safe_question not in questions:
                questions.append(safe_question)
    return bool(fields), questions
