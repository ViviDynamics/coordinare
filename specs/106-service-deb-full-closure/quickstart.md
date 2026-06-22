# Quickstart: State-Independent Service-Deb Closure

## Scenario A — US1 (SC-001): runtime lib fetched despite container pollution
1. Bootstrap container has libicu76 already installed (QA browser pulled it). A postgres service is declared.
2. **Verify:** the service-deb fetch (resolved against the base snapshot) downloads libicu76 into `<cache>/debs/`; the extracted postgres runs (`postgres --version` OK) and spec-101's `pg_isready` passes.

## Scenario B — US2 (SC-002): targeted, no base overlay
1. Same fetch.
2. **Verify:** libc6/libssl3 (in the base image) are NOT downloaded; the set is ~tens of packages, not the full OS closure.

## Scenario C — US3 (SC-003): fallback on older image
1. An image lacking /opt/coordinare-base-dpkg-status runs the rendered command.
2. **Verify:** it falls back to /var/lib/dpkg/status and runs (no error).

## Real-world payoff
The website's postgres now starts at QA: the ICU libraries it needs are present in the cache, so `initdb`/`postgres` load and bind :45432, the spec-101 gate passes with a live DB, and (with 105) the app reaches it via `db`→127.0.0.1.
