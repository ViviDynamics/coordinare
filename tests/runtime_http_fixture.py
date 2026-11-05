"""Host-side HTTP protocol proof for an explicitly owned synthetic carrier."""
from __future__ import annotations

import json
import subprocess
import time
import uuid
from typing import Any

import httpx


def run_http_consumer(command: list[str], *, name: str, metadata: dict[str, Any],
                      receipt: dict[str, Any], expires_at: float) -> dict[str, Any]:
    token = uuid.uuid4().hex
    command[2:2] = ['-d', '-p', '127.0.0.1:0:8088', '-e', 'PERFORMER_AUTH_TOKEN='+token]
    deadline = time.monotonic()+expires_at-time.time()
    result = subprocess.run(command, capture_output=True, text=True, timeout=max(1, deadline-time.monotonic()))
    assert result.returncode == 0, result.stderr
    published = subprocess.check_output(['docker', 'port', name, '8088/tcp'], text=True, timeout=10).strip()
    endpoint = 'http://127.0.0.1:'+published.splitlines()[0].rsplit(':', 1)[1]
    headers = {'Authorization': 'Bearer '+token}
    with httpx.Client(base_url=endpoint, headers=headers, trust_env=False) as client:
        ready_deadline = min(deadline, time.monotonic()+30)
        while time.monotonic() < ready_deadline:
            try:
                status = client.get('/status', timeout=min(5, max(.01, ready_deadline-time.monotonic())))
                if status.status_code == 200:
                    break
            except httpx.TransportError:
                pass
            time.sleep(.1)
        else:
            raise TimeoutError('synthetic HTTP performer did not become ready')
        payload = {'job_id': 'runtime-protocol', 'card_id': 'synthetic', 'role': 'implementing',
            'backend': 'synthetic-fixture', 'persona': 'Run the fixed synthetic protocol fixture',
            'repo_url': 'https://example.invalid/fixtures/synthetic-runtime', 'branch': 'main',
            'metadata': {**metadata, 'environment_runtime_artifacts': receipt}}
        accepted = client.post('/jobs', json=payload, timeout=min(5, max(.01, deadline-time.monotonic())))
        assert accepted.status_code == 202, accepted.text
        job_id = accepted.json()['job_id']
        busy_seen = False
        while time.monotonic() < deadline:
            timeout = min(5, max(.01, deadline-time.monotonic()))
            live = client.get('/status', timeout=timeout)
            live.raise_for_status()
            busy_seen |= live.json()['availability'] == 'busy'
            response = client.get('/jobs/'+job_id, timeout=timeout)
            response.raise_for_status()
            status = response.json()
            if status['state'] in {'succeeded', 'failed', 'cancelled'}:
                assert status['state'] == 'succeeded', json.dumps(status)
                assert busy_seen, 'HTTP control must remain responsive during actual runtime startup/tool use'
                return {'http_job_state': status['state'], 'http_control_busy_observed': busy_seen,
                        'readiness_evidence': False}
            time.sleep(.2)
    raise TimeoutError('synthetic HTTP job exceeded its original fixture expiry')
