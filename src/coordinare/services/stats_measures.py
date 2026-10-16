"""Shared run cohort measures for statistics views."""
from __future__ import annotations

from collections import Counter
from typing import TYPE_CHECKING, Any

from coordinare.services.stats_flow import percentiles
from coordinare.services.token_usage import PriceTable, UsageCounts, price_totals, usage_summary

if TYPE_CHECKING:
    from coordinare.services.token_usage import ModelTotals

OUTCOMES = ("success", "failure", "completed_without_verdict", "blocked", "system_error", "cancelled", "unknown")
MAX_REASON_CHARS = 300


def _measures(
    rows: list[dict[str, Any]], usage: dict[str, ModelTotals], table: PriceTable, top_n: int,
) -> dict[str, Any]:
    outcomes = Counter(row["outcome"] for row in rows)
    verdicts = Counter(row["verdict"] for row in rows if row["verdict"] is not None)
    verdict_runs = sum(verdicts.values())
    resolved = sum(row["resolved_pair"] for row in rows)
    totals: ModelTotals = {}
    missing = 0
    for row in rows:
        run_usage = usage.get(row["run"])
        if not run_usage:
            missing += 1
        for model, counts in (run_usage or {}).items():
            totals[model] = totals.get(model, UsageCounts()) + counts
    summary = usage_summary(totals, table)
    priced = price_totals(totals, table)
    summary["cost"]["usd_exact"] = str(priced.cost_usd) if priced.cost_usd is not None else None
    summary["cost"]["partial_usd_exact"] = str(priced.partial_usd)
    if missing:
        summary["cost"]["usd"] = None
        summary["cost"]["usd_exact"] = None
        summary["cost"]["unknown_reason"] = "Some runs have no complete usage records"
    reasons = Counter(
        str(row.get("reason") or row["outcome"])[:MAX_REASON_CHARS]
        for row in rows
        if row["outcome"] not in {"success", "completed_without_verdict", "unknown"}
    )
    cards = {(row["symphony"], row["card"]) for row in rows}
    return {
        "runs": len(rows),
        "outcomes": {outcome: outcomes[outcome] for outcome in OUTCOMES},
        "success_rate": verdicts["success"] / verdict_runs if verdict_runs else None,
        "verdict_runs": verdict_runs,
        "first_pass_rate": sum(row["first_pass"] for row in rows) / resolved if resolved else None,
        "resolved_card_stages": resolved,
        "runs_per_card": len(rows) / len(cards) if cards else None,
        "retries": sum(row["retry"] for row in rows),
        "bounces": sum(row["bounce"] for row in rows),
        "duration_seconds": percentiles(
            [row["duration_seconds"] for row in rows if row["duration_seconds"] is not None],
        ),
        "last_run_at": max((row["started_at"] for row in rows), default=None),
        "tokens": summary["tokens"],
        "cost": summary["cost"],
        "cost_per_run_usd": (float(priced.cost_usd) / len(rows)
                             if rows and not missing and priced.cost_usd is not None else None),
        "pricing": summary["pricing"],
        "missing_usage_runs": missing,
        "failure_reasons": [
            {"reason": reason, "runs": count}
            for reason, count in sorted(reasons.items(), key=lambda item: (-item[1], item[0]))[
                :top_n
            ]
        ],
    }


