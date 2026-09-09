# Plan

Add an optional SecretStr token to ProjectConfiguration. Validate bind posture at
_run entry. Install a pure ASGI authentication middleware on the dashboard app
so newly added routes are protected automatically without changing handlers or
streaming SSE. Reuse the localhost guard for Host/Origin protection. Browser Basic
avoids a new token store, cookies, login UI or session lifecycle. For SSO, the proxy
authenticates the user and supplies the operator token on its internal hop.

Read-only routes are protected whenever auth is enabled because config/status may
contain operational details. Signed webhook POST uses its separate HMAC auth.
No default token is shipped. Document container and Helm token configuration and
keep the dashboard Service ClusterIP. Test route coverage and startup before CI.
