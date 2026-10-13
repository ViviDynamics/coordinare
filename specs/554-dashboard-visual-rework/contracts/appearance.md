# Appearance contract

System/Light/Dark is local browser state. It does not change REST or SSE schemas.
A head script resolves the effective theme before first paint. The shared shell
exposes a labelled Appearance group with monitor, sun, and moon buttons.
Each button has an accessible name, tooltip, and synchronized aria-pressed state. Cross-tab preference updates
and storage failures must not interrupt dashboard rendering or controls.
Existing page paths, element IDs, navigation actions, and SSE listeners remain.

## Identity resources

GET /static/coordinare-mark.svg returns image/svg+xml;
GET /static/coordinare-conductor.webp returns image/webp. Both return 200 with
original bytes, ETag, and Cache-Control: public, no-cache; matching If-None-Match
returns 304 with no body. The shared shell declares the favicon and reserves
navigation mascot dimensions. No operator state or existing API schema changes.

## Navigation presentation

Desktop links keep their icons in an 80px rail and add labels in a 224px drawer.
Every link keeps an accessible name and title in both states. The toggle exposes
its actual expanded state and an action label. Mobile opening/closing is separate
from saved desktop width. Escape closes the mobile menu and returns focus.

## Header status and links

The shared header shows dashboard-stream state as Connecting, Waiting for updates,
Live, Reconnecting, or Updates quiet. Live requires a received SSE message;
stream opening alone does not imply fresh data. Its tooltip reports the last
received message time. The status text announces transitions, rather than every
heartbeat. Issue and PR links use contrast-tested brass text shades in each theme.


## Account and logout

GET /api/account is protected by the existing dashboard authentication and Host
guard. Returns authentication, name, email, and (for OIDC) subject; Cache-Control:
no-store. It never returns ID/access tokens or client secrets. The top-right
account menu exposes a POST form to /oidc/logout?redirect=1 for OIDC users only.

POST /oidc/logout remains 204 for existing API callers. With redirect=1 it first
revokes the local session and clears session/state/nonce cookies, then returns
303 to validated provider end_session_endpoint with id_token_hint, client_id,
and post_logout_redirect_uri. That return URI is the configured callback origin
plus /oidc/signed-out and must be registered with the provider. Missing/invalid
provider logout metadata falls back to a local signed-out page that explains the
provider session may remain. Existing same-origin checks still apply.

GET /oidc/signed-out is public and uncached, with an explicit Sign in link.
Browser identity is deliberately excluded from the agent MCP operator surface.
