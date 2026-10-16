"""Persona history and recent-run projections for statistics views."""
from __future__ import annotations

from collections import defaultdict
from typing import TYPE_CHECKING, Any

from coordinare.services.persona_history import history_for
from coordinare.services.persona_service import VALID_ROLES
from coordinare.services.stats_flow import instant
from coordinare.services.stats_measures import _measures

if TYPE_CHECKING:
    from datetime import datetime
    from pathlib import Path

    from coordinare.services.run_statistics import StatsFrame
    from coordinare.services.stats_params import StatsQuery
    from coordinare.services.stats_sources import StatsSources
    from coordinare.services.token_usage import PriceTable

RECENT_RUN_LIMIT = 10


def _persona_edits(
    rows: list[dict[str, Any]], start: datetime | None, end: datetime,
) -> dict[str, list[dict[str, Any]]]:
    """Collect distinct edits by UTC day once for both daily and cohort views."""
    edits: dict[str, dict[tuple[Any, ...], dict[str, Any]]] = defaultdict(dict)
    for row in rows:
        at = instant(row.get("persona_changed_at"))
        if at is None or at > end or (start is not None and at < start):
            continue
        key = (row["symphony"], row.get("persona_version"), row.get("persona_sha256"), at)
        edits[at.date().isoformat()][key] = {
            "symphony": key[0], "version": key[1], "sha256": key[2], "changed_at": at.isoformat(),
        }
    return {day: list(bucket.values()) for day, bucket in sorted(edits.items())}


def _recent_runs(
    rows: list[dict[str, Any]], sources: StatsSources, params: StatsQuery, table: PriceTable,
) -> list[dict[str, Any]]:
    recent = []
    for row in sorted(rows, key=lambda row: (row["started_at"], row["run"]), reverse=True)[:RECENT_RUN_LIMIT]:
        measures = _measures([row], sources.usage, table, params.top_n)
        recent.append({
            **{key: row.get(key) for key in (
                "run", "card", "card_title", "agent", "stage", "outcome", "started_at", "duration_seconds",
                "transcript_symphony",
            )},
            # Conductor transcripts live under their own folder, while the display scope is their owner.
            "symphony": row.get("transcript_symphony", row["symphony"]) if params.agent else row["symphony"],
            "attributed_symphony": row["symphony"],
            "cost": measures["cost"], "tokens": measures["tokens"],
            "missing_usage_runs": measures["missing_usage_runs"],
        })
    return recent


def _persona_details(
    rows: list[dict[str, Any]], sources: StatsSources, params: StatsQuery, frame: StatsFrame,
    edits: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    table = frame.table
    versions: dict[tuple[str, Any, Any], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        versions[row["symphony"], row.get("persona_version"), row.get("persona_sha256")].append(row)
    return {
        "persona_versions": [
            {"symphony": key[0], "version": key[1], "sha256": key[2],
             **_measures(group, sources.usage, table, params.top_n)}
            for key, group in sorted(versions.items(), key=lambda item: str(item[0]))
        ],
        "persona_edits": [edit for bucket in edits.values() for edit in bucket],
    }


def _shared_persona_edits(
    root: Path | None, role: str, start: datetime | None, end: datetime,
) -> list[dict[str, Any]]:
    """Metadata only; the shared version store does not record symphony attribution."""
    if root is None or role not in VALID_ROLES:
        return []
    return [
        {"version": version.version, "sha256": version.sha256, "changed_at": version.changed_at,
         "scope": "shared_history"}
        for version in history_for(root).versions(role)
        if (at := instant(version.changed_at)) is not None and at <= end and (start is None or at >= start)
    ]


