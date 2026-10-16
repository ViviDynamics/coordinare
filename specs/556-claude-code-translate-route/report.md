# Issue 223 report

## Result and seam

PR 224 is already in this checkout: commit `244b6c6e`, titled
`223: a format retry starts a fresh session and does not quote a head-cut fragment (#224)`.

The smallest correct seam is the existing spec-084 routing target, not another
backend or config flag. The current checkout already supports the requested
transport. This commit supplies a namespaced example route, documents how to
activate it, and adds regression coverage. Runtime source changes are comments;
no translator rewrite or change to default routing is necessary.

Spec 080 resolves role -> mode -> model endpoint -> endpoint into a model, URL
and credential reference. Spec 122 permits LiteLLM as that endpoint, but does
not automatically select local wire translation. At job start,
`_launch_dispatch_proxy` loads `RoutingTable`; `maybe_launch_proxy` selects a
matching target before ordinary orchestration. `_launch_for_target` starts
`SelfHostedShim`, points `ANTHROPIC_BASE_URL` at loopback, and suppresses
`LITELLM_PROXY_BASE_URL` so the older `ClaudeCodeShim` does not launch too.
Teardown restores both variables. A configured reasoning policy instead uses
the canonical proxy; use a single-model mode without a policy for this route.

The shim translates `/v1/messages` into `/v1/chat/completions`. Its SSE chain
applies declared normalizers before `TranslatingSSEFilter`. That filter already
reads `delta.content` independently of reasoning and opens text blocks lazily.
The exact issue sequence now verifies that the first text is `{"`, reasoning is
dropped, and no text block opens during reasoning-only deltas. Both `reasoning`
and `reasoning_content` are covered, with and without normalizers, including a
split network frame.

## Configuration and garden deployment

No new config key. The activation key is performer env
`SELFHOSTED_ROUTING_CONFIG`, default `""`. The matching routing entry uses
`target.strategy: translate` and `target.wire_format: openai`. An unset path or
unmatched model preserves the current path. Existing Anthropic `observe`
routing is also covered by the dispatch regression.

In the garden `coordinare` ConfigMap's `config.yaml`:

1. Keep the assessor on `backend: claude_code`, using its GLM single-model mode
   without a reasoning policy. The catalog endpoint supplies the LiteLLM URL
   and `auth_env` naming the daemon's gateway credential.
2. Set the selected performer endpoint's
   `env.SELFHOSTED_ROUTING_CONFIG: /etc/coordinare/routing.yaml`, and ensure its
   `roles` includes `assessor`.
3. Copy the `gateway/glm-5.3-flash` entry from `routing.example.yaml`, replacing
   its model with `spark/glm-5.3-flash` for the issue's deployment, and replacing
   its gateway address. Public examples use an illustrative namespace to obey
   the repository's internal-reference guard. Match the dispatched model
   exactly. This example uses
   `upstream_auth_env: ANTHROPIC_AUTH_TOKEN` to reuse the bearer injected from
   the catalog endpoint for probes and traffic. Use `upstream_model` only when
   the gateway expects a different model identifier.
4. Make the file available inside every performer pod. The current Kubernetes
   runtime only turns devenv-cache endpoint volumes into pod mounts; adding a
   Docker-style `host_path` routing volume will not do this. A derived image
   with `COPY routing.yaml /etc/coordinare/routing.yaml` is supported; set the
   endpoint's `image` to that image's immutable tag. Existing cluster pod
   provisioning may instead inject the routing ConfigMap as a volume. A mount
   in the daemon alone is insufficient.
5. Validate both files, restart the daemon and dispatch a fresh assessor.
   Routing loads at job start. Look for
   `selfhosted_layer.translate_launched` and gateway requests to
   `/v1/chat/completions`. Removing the entry reverts this model's routing.

The operator guide includes the separate routing-file validation command.
Daemon config validation does not validate that performer-local file.

## Files changed

- `routing.example.yaml`: copyable translate route with an illustrative model namespace.
- `config.example.yaml`: opt-in recipe, validation rule and Kubernetes caveat.
- `docs/onboarding/05-configuration.md`: activation, defaults and precedence.
- `docs/operators/litellm-proxy.md`: credential reuse and garden deployment steps.
- `agent/performer/src/performer/config.py`: clarify the existing setting.
- `agent/performer/src/performer/proxy/launch.py`: correct the comment that said
  translation excluded LiteLLM entirely; it excludes its Anthropic adapter.
- `agent/performer/src/performer/proxy/translate/sse.py`: state the mixed-delta invariant.
- `tests/unit/test_223_claude_code_translate_route.py`: SSE and real dispatch
  selection regressions, loading the committed example route, including
  startup probe auth, upstream path, model preservation and env restoration.
- `specs/556-claude-code-translate-route/`: brief spec, plan, tasks and this report.

## Analysis and validation

Before implementation, ran the spec prerequisite checker with
`SPECIFY_FEATURE=556-claude-code-translate-route`, then analyzed spec, plan and
tasks against the constitution. Four requirements, four tasks, 100% coverage,
no consistency or contract gaps. No payload fields are added.

- `make lint`: passed.
- Touched performer source lint using its own ruff config: passed.
- `make typecheck`: passed, 317 source files.
- New regressions plus spec-080 catalog/resolution tests: 34 passed, including
  all eight new issue-223 cases.
- Focused translator, request/response, routing, health, Settings and benchmark
  tests: 68 passed. Existing benchmark enforces median <= 5 ms and p99 <= 15 ms.
- Claude Code backend plus reasoning-normalizer tests: 128 passed.
- Entire performer proxy directory plus LiteLLM Settings tests: 346 passed,
  58 failed and 3 setup errors. All 61 failures/errors report sandbox
  `PermissionError` when binding `127.0.0.1:0`; socket tests require an
  unrestricted runner. No tests were disabled or changed to bypass this.
- `python -m coordinare config validate --config config.example.yaml`: passed
  with `COORDINARE_GITHUB_TOKEN=validation-placeholder`, a synthetic value used
  only for offline validation. Without it, the example correctly requires auth.
- `RoutingTable.from_yaml_file("routing.example.yaml")`: passed and resolved
  the issue's model to `translate`.
- Full `make test`: 15,802 passed, 29 failed, 3 skipped, 41 warnings in
  714.97 seconds. Of the failures, 27 are socket-bind permission errors in this
  sandbox. One caught the internal namespace in public examples; the examples
  now use `gateway/`, and the internal-reference guard passes. The other is the
  shard inventory guard, which compares discovered files against `git ls-files`
  and sees the new test as untracked until it is staged.
- After the public-example correction, reran the new regressions, the complete
  BYO-model onboarding tests and the complete shard tests: 49 passed and one
  failed. Only the inventory guard remains; staging is blocked as described below.

Checks used the existing shared dependency environment via an ignored `.venv`
symlink. `PYTHONPATH` explicitly selected this worktree's daemon, performer and
shared routing package rather than the shared environment's editable installs.

## Review and limitations

Reviewed the full diff against default behavior, reasoning loss, text-block
lifecycle, auth precedence, exact model matching, orchestration precedence and
Kubernetes file availability. Executed the corresponding regression checks.
No new runtime logging, credential handling or dependency is introduced.

The full run changed the tracked `events.db`; restored its exact HEAD content.

## Commit blocked by sandbox

Attempted `git add tests/unit/test_223_claude_code_translate_route.py`. Git
failed to create
`/Users/jasonberry/Workspaces/ViviDynamics/coordinare/.git/worktrees/coordinare-wt-223/index.lock`
with `Operation not permitted`. This session has no approval escalation, so
the requested commit could not be created. No Git permission or test guard was
changed to work around that restriction. Changes remain in the worktree.

Once Git metadata is writable, stage the files listed above, rerun
`tests/unit/test_289_ci_shards.py`, and commit with:

```text
223: add the GLM translate route and mixed-delta regression coverage
```

Socket-based tests also need a runner that permits binding local ports.

## Unverified deployment

No GitHub, gateway or garden access was used. The live ConfigMap, gateway model
listing, image contents and deployment result remain unverified. Model names
come from the supplied issue; the example gateway address must be replaced.
This is not a live CLI/model smoke test. The sandbox prevents socket-based
proxy integration tests, so the new dispatch regression mocks server startup
and uses an HTTP mock transport for the real health probe.
