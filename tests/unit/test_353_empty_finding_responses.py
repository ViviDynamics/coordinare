"""An empty finding_responses costs nothing while there are no findings to answer.

Dettmore e2e-90: the author sent "finding_responses": [] beside its first manifest 13 times, the first declaration's
schema forbids the key, and every refusal spent a correction until the schema budget held the attempt.
"""
from __future__ import annotations

from types import SimpleNamespace

from coordinare.services.environment_discovery import _without_empty_responses


def _budget(records=()):
    return SimpleNamespace(finding_ledger=SimpleNamespace(records=list(records)))


def test_an_empty_list_without_findings_is_dropped():
    assert _without_empty_responses({"manifest": {}, "finding_responses": []}, _budget()) == {"manifest": {}}
    assert _without_empty_responses({"manifest": {}, "finding_responses": []},
                                    SimpleNamespace(finding_ledger=None)) == {"manifest": {}}


def test_a_non_empty_list_or_existing_findings_are_validated_as_sent():
    reply = {"manifest": {}, "finding_responses": [{"finding_id": "F0001"}]}
    assert _without_empty_responses(reply, _budget()) == reply
    empty = {"manifest": {}, "finding_responses": []}
    assert _without_empty_responses(empty, _budget(["F0001"])) == empty
