"""Preserve explicit questions in a malformed assessment without assuming readiness."""
from __future__ import annotations

import json
import re

from performer.models import _redact_secrets

_JSON_FIELD = re.compile(r'(?P<key>"(?:[^"\\]|\\.)*")\s*:\s*')
_ASSESSMENT_KEYS = frozenset({"questions", "sufficient", "assessment", "ready"})


def _assessment_fields(raw: str) -> list[tuple[str, int, int]]:
    fields = []
    for field in _JSON_FIELD.finditer(raw):
        try:
            key = json.loads(field.group("key"))
        except ValueError:
            continue
        if key in _ASSESSMENT_KEYS:
            fields.append((key, field.start(), field.end()))
    return fields


def assessment_fields_outside_object(raw: str) -> bool:
    """A nested object is not an assessment when its contract fields lie outside it."""
    start, end = raw.find("{"), raw.rfind("}")
    return any(
        start < 0 or field_start < start or field_end > end
        for _, field_start, field_end in _assessment_fields(raw)
    )


class _ObjectPairs(list[tuple[str, object]]):
    """Keep object fields distinct from arrays and retain duplicate keys."""


def _assessment_pairs(raw: str) -> _ObjectPairs | None:
    start, end = raw.find("{"), raw.rfind("}")
    if start >= 0 and end > start:
        try:
            value = json.loads(raw[start:end + 1], object_pairs_hook=_ObjectPairs)
            return value if isinstance(value, _ObjectPairs) else None
        except ValueError:
            pass
    return None


def _contract_objects(pairs: _ObjectPairs) -> list[_ObjectPairs]:
    return [pairs, *[
        value for key, value in pairs
        if key == "assessment" and isinstance(value, _ObjectPairs)
    ]]


def assessment_has_duplicate_contract_fields(raw: str) -> bool:
    """Reject ambiguous assessment fields while ignoring unrelated metadata."""
    pairs = _assessment_pairs(raw)
    if pairs is None:
        return False
    for index, obj in enumerate(_contract_objects(pairs)):
        relevant = _ASSESSMENT_KEYS if index == 0 else {"ready", "questions", "verdict"}
        keys = [key for key, _ in obj if key in relevant]
        if len(keys) != len(set(keys)):
            return True
    return False


def _question_candidates(raw: str, fields: list[tuple[str, int, int]]) -> list[object]:
    pairs = _assessment_pairs(raw)
    if pairs is not None and not assessment_fields_outside_object(raw):
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


def assessment_fragment_questions(raw: str) -> tuple[bool, list[str]]:
    """Identify contract fragments; recover only complete arrays of question strings."""
    fields = _assessment_fields(raw)
    questions: list[str] = []
    for value in _question_candidates(raw, fields):
        if not isinstance(value, list) or isinstance(value, _ObjectPairs) or any(not isinstance(q, str) for q in value):
            continue
        for question in value:
            safe_question = _redact_secrets(question)
            if safe_question.strip() and safe_question not in questions:
                questions.append(safe_question)
    return bool(fields), questions
