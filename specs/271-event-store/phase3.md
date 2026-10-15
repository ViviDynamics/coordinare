# Phase 3 export

Scope is outbound OTLP logs, Prometheus metrics and Helm documentation. No Logs
page, MCP changes or event schema migrations are included. Leave work uncommitted.

## Design and analysis

The read only speckit analysis used the existing spec, plan, tasks and constitution.
The original plan and tasks describe phase 1 only. This supplement maps the phase 3
requirements from issue section 5 to implementation and tests. No cross performer
contract changes are needed. There are no unresolved scope questions.

Use the existing httpx dependency for OTLP HTTP JSON by default. HTTP protobuf and
gRPC need generated OTLP protobuf messages and gRPC transport bindings, provided by
an optional otlp extra. Do not implement a protobuf codec or gRPC framing by hand.
No SDK, instrumentation hooks or global OpenTelemetry provider is needed.

Environment values override YAML field by field. Signal specific values override
general values, including a configured logs endpoint overriding a general endpoint.
A general HTTP endpoint gets /v1/logs appended; a logs endpoint is used verbatim.
The selected protocol is never silently substituted. Invalid YAML telemetry settings
fail validation at save and startup (#225). Invalid environment overrides disable
only export with a safe error. Empty OTEL_SERVICE_NAME uses the default, coordinare.
Auth headers cannot use a proxy for plain HTTP, even with insecure enabled.

After committing new rows, the writer hands only redacted rows to an independent
worker thread. Replay rows ignored by SQLite are not exported again. Export errors
cannot cause SQLite retries. Both count and byte budgets include the batch in flight.
Defaults: 1024 rows, 4 MiB, batches of 128, one second flush, three second timeout,
three attempts, exponential backoff from one second capped at thirty seconds. Shutdown
is bounded and counts discarded rows. Collector response bodies are bounded too.
Data is a JSON string capped at 16 KiB with valid JSON retained on truncation.

Use the existing Prometheus registry and health listener. The application route is
enabled by default and can be disabled with observability.metrics_enabled: false.
Helm's metrics.expose controls only Service publication and defaults to false.
Anyone who reaches the health port can read metrics; isolate it with network policy. No dashboard auth
is granted or reused. Existing cost and token metrics remain available.

Run counters are incremented on accepted dispatch and on leaving the common monitor,
using the stage captured before advancement. Outcomes distinguish completed, blocked,
system error and redispatch. Bootstrap outcomes come from the canonical completion
log, independent of the optional activity feed. Metric labels are bounded enums and
contain no card, run, symphony or arbitrary event strings.

## Tasks and validation mapping

1. Write failing fake collector, configuration, metrics and Helm tests.
2. Implement validated config and environment precedence, including protocol selection.
3. Implement resource and record mapping, bounded thread delivery, batching, timeout,
   retry backoff, bounded responses and loss counters.
4. Hook committed redacted rows and lifecycle into existing common paths.
5. Render settings, document Collector to Loki and Prometheus to Grafana deployment.
6. Verify redaction, shape, severity, batching, failure isolation, retry, saturation,
   shutdown, partial rejection, precedence, opt out and chart rendering in unit tests.
7. Run touched file lint, package mypy, the ruff ratchet, relevant unit and node tests.
   Binding sockets and commits are prohibited in this worktree.

## Initial verification results

The first test run exited 2 because the exporter module did not exist. Regression
cases also reproduced invalid endpoint and header acceptance and uppercase gRPC
metadata keys before their fixes.

Final checks used the requested shared virtual environment and PYTHONPATH layout.

* Touched Python file ruff check: exit 0.
* mypy package check: exit 0, 270 source files.
* scripts/ruff_baseline.py: exit 0, unchanged baseline holds.
* Relevant unit suite: exit 0, 641 passed, including all 47 new export tests.
* Dashboard decomposition and ruff baseline unit tests: exit 0, 15 passed.
* Shipped System health node checks: exit 0, SUMMARY 50 0.
* Optional wire tests with cached real protobuf and gRPC packages: exit 0,
  7 passed. HTTP uses MockTransport and gRPC channels are mocked; no sockets.
* Helm lint: exit 0. Default, configured, override and secret placement rendering
  is covered in the unit suite.
* Added line restrictions, future imports, module imports and HTML size: exit 0.
* git diff whitespace check: exit 0.

The network could not reach PyPI. The optional dependency lock resolved offline
from cached metadata. Cached packages were copied into a temporary directory to
exercise the optional wire paths without changing the shared virtual environment.
Live server tests and the new Chromium and Firefox E2E test were not run because
socket binding is prohibited. All changes remain in the working tree.

## Revision verification

Regression tests were written before the fixes. They reproduced the token scheme
leak, empty service name rejection, cleartext proxy auth acceptance and the replay
fallback's dependence on a fixed column order. YAML protocol errors now fail
validation like other YAML errors, while invalid environment overrides still
disable only export. Startup placeholder errors were checked with secret canaries.

The focused revision, prior review and activity detail tests passed: 81 tests,
exit 0. The requested unit selection finished with 743 passed and one failure,
exit 1: the restored MCP metrics contract test cannot bind a socket in this
sandbox. The MCP contract files match origin/main exactly. The transport smoke
file restores its socket path with only httpx moved to a module import to follow
the import rule. The added in process transport was removed. No socket coverage
was replaced or disabled. Real socket verification remains for the operator.

Touched Python lint, package mypy (270 files) and the unchanged ruff ratchet all
exited 0. The ratchet holds at 1417 violations across 43 rules. The schema is
still v3; phase 2 owns the v4 indexes and ignored insert drop accounting.

## Changed files

Runtime and configuration:

* src/coordinare/services/otlp_settings.py
* src/coordinare/services/otlp_wire.py
* src/coordinare/services/otlp_export.py
* src/coordinare/services/export_metrics.py
* src/coordinare/services/event_store.py
* src/coordinare/config.py
* src/coordinare/__main__.py
* src/coordinare/health.py
* src/coordinare/graph/nodes/dispatch_performer.py
* src/coordinare/graph/nodes/monitor_performer.py
* src/coordinare/dashboard/dashboard_static/systemhealth.js
* pyproject.toml
* uv.lock

Deployment and documentation:

* deploy/helm/coordinare/values.yaml
* deploy/helm/coordinare/templates/configmap.yaml
* deploy/helm/coordinare/templates/statefulset.yaml
* deploy/helm/coordinare/README.md
* README.md
* docs/security/threat-model.md
* specs/271-event-store/phase3.md

Tests:

* tests/unit/test_271_otlp.py
* tests/unit/test_271_otlp_wire.py
* tests/unit/test_271_export_metrics.py
* tests/unit/test_271_export_chart.py
* tests/unit/test_271_capture.py
* tests/unit/test_551_mcp_contract.py
* tests/unit/test_health_coverage.py
* tests/js/system_health_checks.js
* tests/e2e/test_271_export_health.py
