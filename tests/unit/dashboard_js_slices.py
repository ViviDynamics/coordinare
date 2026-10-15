"""Shared slices of the shipped dashboard JS for the node-based checks.

Several tests slice one function out of ``performers.js`` and run it under
node. Since #136 and #137 those functions look their labels up in the
generated glossary, so each slice needs the same preamble.
"""
from __future__ import annotations

import re

from coordinare.dashboard import _DASHBOARD_HTML, _DASHBOARD_JS_SOURCES

# 176: the application script that used to be the page's inline block.
APP_SCRIPT = "app"


def app_js() -> str:
    """The application script (176), verbatim from the shipped static file."""
    return _DASHBOARD_JS_SOURCES[APP_SCRIPT]


def page_source() -> str:
    """The markup and the application script, for checks that used to read one string.

    Every substring that sat in the old inline page still sits in here, so a
    check written against the page keeps its meaning.
    """
    return _DASHBOARD_HTML + app_js()


_LABEL_HELPERS = (
    "vocabularyLabel", "vocabularyKindLabel", "stageLabel", "agentLabel",
    "agentForStage", "stageAgentLabel", "personaStageNote", "roleOrStageLabel",
)


def shipped_var(source_name: str, var_name: str) -> str:
    """One top-level ``var NAME = ...;`` line, verbatim, from a shipped dashboard script (233)."""
    source = _DASHBOARD_JS_SOURCES[source_name]
    match = re.search(rf"^var {var_name} = .*;$", source, re.MULTILINE)
    if match is None:
        raise AssertionError(f"{var_name} is no longer declared in {source_name}.js")
    return match.group(0)


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


def run_id_js() -> str:
    """141: the run's short name and its markup (helpers.js), verbatim.

    Assumes ``esc`` is already defined by the caller's slice. Any slice that renders a run link needs it.
    """
    return "\n\n".join([shipped_function("helpers", "runIdText"), shipped_function("helpers", "runIdHtml")])


def time_helpers_js() -> str:
    """157: the one time helper set (helpers.js): the zone label, the exact time, the age and timeHtml.

    Assumes ``esc`` is already defined by the caller's slice.
    """
    return "\n\n".join(
        shipped_function("helpers", name) for name in ("timeMs", "zoneLabel", "fmtTime", "fmtAgo", "fmtAge", "fmtAgeAt", "timeHtml", "refreshRelativeTimes")
    )


def symphony_switcher_js() -> str:
    """139: the symphony switcher's pure functions, verbatim, with esc() in front of them."""
    return "\n\n".join([shipped_function("helpers", "esc"), _DASHBOARD_JS_SOURCES["symphonies"]])


def usage_helpers_js() -> str:
    """134: the token and cost formatting (usage.js) and the performers.js lookup that feeds it.

    Assumes ``esc`` and ``fmtCost`` are already defined by the caller's slice.
    """
    return "\n\n".join([
        _DASHBOARD_JS_SOURCES["usage"],
        shipped_function("performers", "usageSummaryOf"),
    ])
