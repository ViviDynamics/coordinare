"""Account identity and provider logout use real signed-token login round trips."""
from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from tests.unit.dashboard.oidc_double import CLIENT_ID, ISSUER, SUBJECT, ProviderDouble
from tests.unit.test_497_oidc_session import BASE, _app, _log_in


class ProfileProvider(ProviderDouble):
    def __init__(self, endpoint: str | None = f'{ISSUER}/end-session?tenant=preview') -> None:
        super().__init__()
        self.endpoint = endpoint
        self.last_token = ''

    def discovery(self) -> dict[str, str]:
        result = super().discovery()
        if self.endpoint is not None:
            result['end_session_endpoint'] = self.endpoint
        return result

    def id_token(self, **overrides: object) -> str:
        self.last_token = super().id_token(name='Sample Operator', email='operator@example.test', **overrides)
        return self.last_token


def test_account_uses_verified_profile_without_exposing_tokens() -> None:
    provider = ProfileProvider()
    with TestClient(_app(provider), base_url=BASE) as client:
        assert client.get('/api/account').status_code == 401
        _log_in(client, provider)
        response = client.get('/api/account')
        assert response.status_code == 200
        assert response.json() == {
            'authentication': 'oidc', 'name': 'Sample Operator',
            'email': 'operator@example.test', 'subject': SUBJECT,
        }
        assert response.headers['cache-control'] == 'no-store'
        assert provider.last_token not in response.text


def test_browser_logout_revokes_cookie_and_session_then_redirects_to_provider() -> None:
    provider = ProfileProvider()
    with TestClient(_app(provider), base_url=BASE) as client:
        _log_in(client, provider)
        old_cookie = client.cookies.get('coordinare_session')
        response = client.post('/oidc/logout?redirect=1', headers={'Origin': BASE}, follow_redirects=False)
        assert response.status_code == 303
        url = urlparse(response.headers['location'])
        assert url.netloc == 'provider.test' and url.path == '/end-session'
        query = parse_qs(url.query)
        assert query['id_token_hint'] == [provider.last_token]
        assert query['client_id'] == [CLIENT_ID]
        assert query['post_logout_redirect_uri'] == [BASE + '/oidc/signed-out']
        assert query['tenant'] == ['preview']
        assert response.headers['cache-control'] == 'no-store'
        assert not client.cookies.get('coordinare_session')
        # Replaying the previous cookie must not restore access.
        assert client.get('/api/account', headers={'Cookie': f'coordinare_session={old_cookie}'}).status_code == 401
        page = client.get('/oidc/signed-out', follow_redirects=False)
        assert page.status_code == 200 and 'Sign in' in page.text


@pytest.mark.parametrize('endpoint', [None, 'http://provider.test/logout', 'https://user:secret@provider.test/logout', 'https://provider.test/logout#fragment'])
def test_missing_or_unsafe_logout_endpoint_still_ends_local_session(endpoint: str | None) -> None:
    provider = ProfileProvider(endpoint)
    with TestClient(_app(provider), base_url=BASE) as client:
        _log_in(client, provider)
        response = client.post('/oidc/logout?redirect=1', headers={'Origin': BASE}, follow_redirects=False)
        assert response.status_code == 303
        assert response.headers['location'] == '/oidc/signed-out?local=1'
        assert client.get('/api/account').status_code == 401


def test_foreign_origin_cannot_log_out_signed_in_user() -> None:
    provider = ProfileProvider()
    with TestClient(_app(provider), base_url=BASE) as client:
        _log_in(client, provider)
        response = client.post('/oidc/logout?redirect=1', headers={'Origin': 'https://foreign.test'}, follow_redirects=False)
        assert response.status_code == 403
        assert client.get('/api/account').status_code == 200


def test_unauthenticated_visits_get_a_public_landing_page_without_starting_sso() -> None:
    provider = ProfileProvider()
    with TestClient(_app(provider), base_url=BASE) as client:
        response = client.get('/', follow_redirects=False)
        assert response.status_code == 302
        assert response.headers['location'] == '/login'
        landing = client.get('/login', follow_redirects=False)
        assert landing.status_code == 200
        assert 'Sign in to your dashboard' in landing.text
        assert 'href="/oidc/login"' in landing.text
        assert '/static/coordinare-conductor.webp' in landing.text
        assert landing.headers['cache-control'] == 'no-store'
        assert 'EventSource' not in landing.text
        assert client.cookies.get('oidc_state') is None
        assert client.get('/static/coordinare-mark.svg').status_code == 200
        assert client.get('/static/coordinare-conductor.webp').status_code == 200
        assert client.get('/api/state').status_code == 401
        assert client.get('/api/account').status_code == 401
        assert client.get('/events', follow_redirects=False).status_code == 401
        # The operator shell is still protected; only branding is public.
        assert client.get('/static/shell.css', follow_redirects=False).status_code == 302
        _log_in(client, provider)
        dashboard = client.get('/', follow_redirects=False)
        assert dashboard.status_code == 200 and 'id="main-content"' in dashboard.text
