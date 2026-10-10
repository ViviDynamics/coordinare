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


def assessment_fragment_questions(raw: str) -> tuple[bool, list[str]]:
    """Identify contract fragments; recover only complete arrays of question strings."""
    fields = _assessment_fields(raw)
    questions: list[str] = []
    for key, _, end in fields:
        if key != "questions":
            continue
        try:
            value, _ = json.JSONDecoder().raw_decode(raw[end:])
        except ValueError:
            continue
        if not isinstance(value, list) or any(not isinstance(q, str) for q in value):
            continue
        for question in value:
            safe_question = _redact_secrets(question)
            if safe_question.strip() and safe_question not in questions:
                questions.append(safe_question)
    return bool(fields), questions
