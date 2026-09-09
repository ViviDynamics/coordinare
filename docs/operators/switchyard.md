# Optional Switchyard evaluation sidecar

Switchyard 0.2.0's server is explicitly a **demo, not for production use**. This
integration is an opt-in evaluation path. It is absent from all default configs
and the default compose stack. Keep static routing unless your own measurements
justify this additional service. Upstream: https://github.com/NVIDIA-NeMo/Switchyard.

## Start the service

Export `LITELLM_MASTER_KEY` from your operator secret store. Review
`examples/switchyard/routes.toml`: all weak/strong/judge calls go through LiteLLM,
which receives the real model IDs for accounting. Replace the example gateway URL
and weak/strong model placeholders with IDs served by your gateway. Set
`SWITCHYARD_ROUTES_FILE` to an absolute path to use a separate routes file.

Run `docker compose -f examples/switchyard/compose.yaml up -d --build`.
The Rust build is pinned to 0.2.0. The service binds host loopback by default;
it has no inbound authentication. Do not expose it publicly. For bridged
performers, bind a host bridge interface reachable as `host.docker.internal`
using `SWITCHYARD_BIND_ADDRESS`, restrict it to trusted containers with the host
firewall, and add `extra_hosts: ["host.docker.internal:host-gateway"]` on Linux.
Use a protected TLS proxy if crossing machines. Never forward vendor credentials
to the sidecar; it owns only the LiteLLM gateway key.

Export `SWITCHYARD_API_KEY=local-evaluation-no-auth` for the local demo server
(or your dedicated proxy credential if you put authentication in front of it).
The catalog injects that value as the performer's `ANTHROPIC_AUTH_TOKEN`, and
the routing shim replaces all CLI auth headers with it. This prevents a native
vendor key from being forwarded to the sidecar.

Merge `examples/switchyard/catalog.yaml` into an example-based configuration.
Mount `examples/switchyard/routing.yaml` into the selected performer and set
`SELFHOSTED_ROUTING_CONFIG` to that mounted path. Build a current performer image.
Validate with `python -m coordinare config validate` before starting the daemon.
The normal 099 completion health probe runs at job startup. A dead sidecar fails
that probe before the CLI starts; it does not silently fall back to static routing.

## Sessions and observations

The per-job translation shim generates one random `x-switchyard-session-id` and
uses it on every request for that job, overriding any CLI-provided value. Another
job gets another identity. Health probes carry no session identity and cannot
pre-escalate the job. Do not use direct `reroute`: it bypasses header injection,
and config validation rejects that combination. This example uses `single` mode
with a translated Claude harness. Reasoning policies use a different proxy path
and are explicitly rejected with this session-header option. Direct health-fallback
reroutes are also rejected, because they would discard session identity.

With two confirmations the first weak answer can be returned even when the judge
flags it. Once escalation latches, it stays strong for the rest of the session.
Each unlatched turn costs a judge call; the latching turn can cost weak + judge +
strong. The example uses the strong model as judge, so savings are not assumed.

The `switchyard-logs` volume contains `routing.jsonl`, with tier/model/usage per
upstream request. Count weak, strong and classifier calls separately. Session IDs
are null in these v0.2.0 records; do not claim per-session rates from that field.
The service log’s `LLM request handled` records carry `session_id` and
`selected_model`; group nonempty session IDs to count the served tiers for a job.
Empty IDs identify the startup health probes. Correlate usage rows by controlled
run timestamps, since the JSONL itself cannot assign them to a session.
Also collect `docker compose -f examples/switchyard/compose.yaml logs switchyard`.
Alert on `judge verdict unavailable`, `parse_error`, and upstream errors: an
HTTP 200 with zero escalation can mean the judge stopped parsing. A nonzero parse
failure rate invalidates a claimed routing-quality comparison. `/metrics` alone
does not provide reliable escalation-tier attribution in this release.

Stop with `docker compose -f examples/switchyard/compose.yaml down`; omit `-v` to
retain measurement logs. To disable routing, restore the prior performer mode and
remove its routing-table entry, then restart. No default file needs changing.
