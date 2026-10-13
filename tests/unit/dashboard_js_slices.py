"""Shared slices of the shipped dashboard JS for the node-based checks.

Several tests slice one function out of ``performers.js`` and run it under
node. Since #136 and #137 those functions look their labels up in the
generated glossary, so each slice needs the same preamble.
"""
from __future__ import annotations

import re

from coordinare.dashboard import _DASHBOARD_JS_SOURCES

_LABEL_HELPERS = (
    "vocabularyLabel", "vocabularyKindLabel", "stageLabel", "agentLabel",
    "agentForStage", "stageAgentLabel", "personaStageNote", "roleOrStageLabel",
)


def shipped_function(source_name: str, function_name: str) -> str:
    """One top-level function, verbatim, from a shipped dashboard script."""
    source = _DASHBOARD_JS_SOURCES[source_name]
    match = re.search(rf"function {function_name}\(.*?\n\}}", source, re.DOTALL)
    if match is None:
        raise AssertionError(f"{function_name}() is no longer in {source_name}.js")
    return match.group(0)


def label_helpers_js() -> str:
    """The generated glossary plus every label lookup in ``helpers.js``.

    Assumes ``esc`` is already defined by the caller's slice.
    """
    return "\n\n".join(
        [_DASHBOARD_JS_SOURCES["vocabulary"]]
        + [shipped_function("helpers", name) for name in _LABEL_HELPERS],
    )
