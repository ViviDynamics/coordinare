from __future__ import annotations

import io
import json
import logging
import re
import subprocess
from types import SimpleNamespace

import pytest

from coordinare.dashboard import _DASHBOARD_HTML, DashboardStore
from coordinare.lib.redaction import RedactingFormatter, redact_mapping


@pytest.mark.parametrize('enabled', [False, True])
def test_optional_assistant_does_not_abort_quiet_timer(enabled):
    match = re.search(r"document.addEventListener\('DOMContentLoaded', function\(\) \{(.*?)\n\}\);", _DASHBOARD_HTML, re.DOTALL)
    assert match
    script = '''const assert = require('node:assert/strict');
let routed = false, timer = false, assistant = false;
function router() { routed = true; }
function afTick() {}
function setInterval(fn, ms) { assert.equal(fn, afTick); assert.equal(ms, 5000); timer = true; }
'''
    if enabled:
        script += 'function assistantAvailable() { assistant = true; }\n'
    script += match.group(1)
    script += f'\nassert(routed && timer); assert.equal(assistant, {json.dumps(enabled)});'
    result = subprocess.run(['node', '-e', script], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(('ready', 'success', 'in_flight'), [(True, True, False), (False, False, False), (False, None, True)])
def test_symphony_snapshot_carries_bootstrap_state(ready, success, in_flight):
    cache = SimpleNamespace(cache_dir_ready=ready, last_bootstrap_succeeded=success,
                            bootstrap_in_flight=in_flight, last_bootstrap_error='failed' if success is False else None)
    daemon = SimpleNamespace(state={'symphony_configs': {'website': SimpleNamespace()}, 'env_cache': {'website': cache}})
    entry, = DashboardStore._build_symphonies_data(daemon)
    assert entry['cache_dir_ready'] is ready
    assert entry['last_bootstrap_succeeded'] is success
    assert entry['bootstrap_in_flight'] is in_flight
    assert entry['last_bootstrap_error'] == cache.last_bootstrap_error


def test_third_party_log_redacts_formatted_url_and_exception():
    url = 'https://hooks.slack.com/services/T_SYNTHETIC/B_SYNTHETIC/SYNTHETIC_SECRET'
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(RedactingFormatter(logging.Formatter('%(levelname)s %(message)s')))
    logger = logging.Logger('httpx.synthetic')
    logger.addHandler(handler)
    logger.info('HTTP Request: POST %s "HTTP/1.1 200 OK"', url)
    try:
        raise ValueError('request failed: ' + url)
    except ValueError:
        logger.exception('transport error')
    output = stream.getvalue()
    assert 'SYNTHETIC_SECRET' not in output
    assert '200 OK' in output and 'ValueError' in output
    assert '[REDACTED]' in output
    assert 'SYNTHETIC_SECRET' not in str(redact_mapping({'event': 'POST ' + url}))
