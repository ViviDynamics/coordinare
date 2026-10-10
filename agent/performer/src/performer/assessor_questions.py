"""Preserve explicit questions in a malformed assessment without assuming readiness."""
from __future__ import annotations

import json
import re

_ASSESSMENT_FIELD = re.compile(r'"(questions|sufficient|assessment|ready)"\s*:\s*')


def assessment_fragment_questions(raw: str) -> tuple[bool, list[str]]:
    """Identify contract fragments; recover only complete arrays of question strings."""
    fields = list(_ASSESSMENT_FIELD.finditer(raw))
    questions: list[str] = []
    for field in fields:
        if field.group(1) != "questions":
            continue
        try:
            value, _ = json.JSONDecoder().raw_decode(raw[field.end():])
        except ValueError:
            return True, []
        if not isinstance(value, list) or any(not isinstance(q, str) for q in value):
            return True, []
        for question in value:
            if question.strip() and question not in questions:
                questions.append(question)
    return bool(fields), questions
