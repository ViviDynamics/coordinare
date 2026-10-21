"""Opt-in check that a provider can compile every strict discovery schema at its largest bounds.

Providers reject oversized structured-output grammars before generating anything, which no
local test observes. Each probe sends a minimal prompt with no repository content.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

import httpx

from coordinare.services.conducting import SCHEMA_LIMIT_MARKERS
from coordinare.services.environment_response_schema import (
    constrain_source_criteria,
    response_schema,
)
from coordinare.services.environment_structured_wire import wire_schema

MAX_DECISIONS = 128
MAX_CRITERIA = 512


def _schemas() -> dict[str, dict[str, Any]]:
    decision_ids = [f'F{i:04}' for i in range(1, MAX_DECISIONS+1)]
    return {
        'scope': response_schema('scope'),
        'scope_revision': response_schema('scope_revision'),
        'scope_assessment': constrain_source_criteria(response_schema('scope_assessment', review_decision_ids=decision_ids),
                                                      [f'S{i:016x}' for i in range(MAX_CRITERIA)]),
        'assessment': response_schema('assessment', review_decision_ids=decision_ids),
        'probe': response_schema('probe'),
        'manifest': response_schema('manifest'),
    }


def probe_requests(model: str) -> dict[str, dict[str, Any]]:
    return {phase: {'model': model, 'max_tokens': 32,
                    'messages': [{'role': 'user', 'content': 'Reply with a minimal valid object.'}],
                    'response_format': {'type': 'json_schema', 'json_schema': {
                        'name': 'coordinare_environment_response', 'strict': True, 'schema': wire_schema(schema)}}}
            for phase, schema in _schemas().items()}


def classify(status: int, text: str) -> str:
    """Report only a bounded outcome; provider bodies are never echoed."""
    if status == 200:
        return 'ok'
    return 'grammar_too_large' if any(marker in text.lower() for marker in SCHEMA_LIMIT_MARKERS) else f'http_{status}'


def _post(url: str, body: dict[str, Any], key: str) -> tuple[int, str]:
    response = httpx.post(url, json=body, headers={'Authorization': 'Bearer ' + key}, timeout=120)
    return response.status_code, response.text


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-url', required=True)
    parser.add_argument('--model', required=True)
    parser.add_argument('--key-env', required=True, help='name of the environment variable holding the API key')
    args = parser.parse_args(argv)
    key = os.environ.get(args.key_env)
    if not key:
        parser.error(f'environment variable {args.key_env} is not set')
    url = args.base_url.rstrip('/') + '/chat/completions'
    results = {phase: classify(*_post(url, body, key)) for phase, body in probe_requests(args.model).items()}
    sys.stdout.write(json.dumps(results, indent=2) + "\n")
    return 0 if all(value == 'ok' for value in results.values()) else 1


if __name__ == '__main__':
    sys.exit(main())
