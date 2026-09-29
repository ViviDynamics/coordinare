# Data Model

## DashboardOidcConfig (new pydantic model, on ProjectConfiguration)

| Field | Type | Default | Validation |
| --- | --- | --- | --- |
| `discovery_url` | `str` | required | `https://` scheme; host non-empty |
| `client_id` | `str` | required | non-empty |
| `client_secret` | `SecretStr` | required | non-empty; never rendered, logged, or echoed |
| `redirect_url` | `str` | required | absolute URL; host ∈ {dashboard host} ∪ trusted_dashboard_hosts |
| `session_hours` | `int` | 12 | 1–168 |

Presence of the block enables OIDC. Absence leaves the daemon byte-identical to spec 143.

## Session (in-memory record)

| Field | Type | Semantics |
| --- | --- | --- |
| `cookie_value` | `str` (index) | 256-bit random, url-safe; lookup key only |
| `subject` | `str` | provider's `sub` claim, display only |
| `expires_at` | `datetime` (UTC) | absolute; `now >= expires_at` ⇒ refused and evicted |

State transitions: `created` (on verified exchange) → `expired` (at expiry or daemon
restart) or `revoked` (on logout). No persistence; no refresh.

## OIDC exchange state (short-lived, cookie-carried)

| Field | Type | Semantics |
| --- | --- | --- |
| `state` | `str` | random per login; compared at callback, then consumed |
| `nonce` | `str` | random per login; must equal ID-token `nonce` claim |
| `set_at` | `datetime` | cookie max-age bound (5 min); stale ⇒ refused |

## Redaction posture

`client_secret` joins `dashboard_auth_token` in the existing secret-redaction set: excluded
from `/api/config*` responses, from `coordinare config` validation output, and from log
records. Tests assert a distinctive marker value never appears in any emitted payload.
