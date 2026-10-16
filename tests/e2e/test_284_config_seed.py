from __future__ import annotations

import json

import pytest
from playwright.sync_api import Page, expect


@pytest.mark.e2e
def test_config_page_shows_seed_drift_and_explicit_reset_instructions(page: Page, live_server_url: str) -> None:
    page.route('**/api/config/all', lambda route: route.fulfill(
        content_type='application/json', body=json.dumps({
            'sections': [], 'config_version': 1, 'content_hashes': {},
            'config_seed': {
                'chart_changed': True,
                'message': 'chart values changed since this config was seeded',
                'changed_sections': ['github_org', '<img src=x onerror=alert(1)>'],
                'live_diff_sections': ['symphonies'],
                'reset_instruction': 'Back up the live config, create config/reseed on the state PVC, then restart the Pod.',
            },
        }),
    ))
    page.goto(f'{live_server_url}/config')
    banner = page.locator('#cfg-seed-status')
    expect(banner).to_contain_text('chart values changed since this config was seeded')
    expect(banner).to_contain_text('github_org')
    expect(banner).to_contain_text('symphonies')
    expect(banner).to_contain_text('config/reseed')
    expect(banner.locator('img')).to_have_count(0)
