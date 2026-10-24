"""Real performer HTTP routes with a synthetic executor and actual runtime tools."""
from __future__ import annotations

import os

import uvicorn
from driver import run
from performer.server import create_app
from performer.server.models import JobInitPayload, JobResult, PerformerCapabilities


async def execute(payload: JobInitPayload) -> JobResult:
    assert payload.role == 'implementing'
    await run('live_consumer', contract=payload.metadata)
    return JobResult(success=True, summary='Synthetic HTTP/runtime protocol proof; never project readiness')


if __name__ == '__main__':
    uvicorn.run(create_app(expected_token=os.environ['PERFORMER_AUTH_TOKEN'], executor=execute,
        capabilities=PerformerCapabilities(backends=['synthetic-fixture'])), host='0.0.0.0', port=8088)
