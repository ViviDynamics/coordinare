"""Closer judge step (spec 172 FR-004): at most one schema-guarded model call.

Called only when code could not classify every thread. Threads beyond the
per-call cap stay open rather than being judged unseen. One nonce per call
fences every thread and is named in the rule, so the two always agree (spec 178).
"""
from __future__ import annotations

from pydantic import BaseModel

from performer.workflows.budget import Budget
from performer.workflows.closer.models import Thread, model_judgements_schema
from performer.workflows.closer.personas import JUDGE_PERSONA, render_threads
from performer.workflows.untrusted import new_nonce, untrusted_rule

__all__ = ["run_judge_step"]


async def run_judge_step(toolkit, threads: list[Thread], max_threads: int) -> BaseModel:
    schema = model_judgements_schema(max_threads)
    nonce = new_nonce()
    persona = JUDGE_PERSONA.format(rule=untrusted_rule(nonce), threads=render_threads(threads[:max_threads], nonce))
    content = [{"type": "text", "text": "Judge each thread above and return the JSON."}]
    return await toolkit.call_model(persona=persona, schema=schema, content=content, budget=Budget.for_step("closing_judge"))
