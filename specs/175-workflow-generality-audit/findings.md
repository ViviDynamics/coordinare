# Performer workflow generality audit (2026-09-13)

Scope: every spec-164 lane (assessor, architect, implementer, reviewer, security, qa, documenter, closer, advocate, curator), the shared workflow plumbing, and the coordinare-side inputs (ci_detection, env manifest, personas, local test gate). Method: one read-only auditor per lane against 12 issue archetypes (schema-first, new endpoint, bug with repro, refactor, docs-only, CI/config-only, frontend-only, dependency bump, flaky test, monorepo, non-web CLI/library, one-line issue) and 7 stacks (Rails baseline, Python, Node/TS, Go, Java, .NET, Rust). Every BLOCKER below was re-read at the cited line by the consolidating session before inclusion; "inferred" marks claims not traced end to end.

Legend: B = blocker (lane cannot complete or produces a wrong verdict), D = degraded, C = cosmetic.

## 1. Implementer (core) — verified

- B1 Test command discovery knows four stacks only. `packages/ci_detection/src/coordinare_ci_detection/__init__.py:197` `for detector in (_detect_ruby, _detect_python, _detect_node, _detect_make)`. Root-only probes. Go/Rust/Java/.NET and any monorepo without a root manifest raise `NoTestRunner` (`implementer/baseline.py:96-98`) which becomes `env_blocked` before the first turn.
- B2 The two operator overrides for the test command are dead. `implementer/baseline.py:77-83` reads `score.local_test_gate_config` and `score.test_command`; `Score` (`performer/models.py`, extra="ignore") declares neither, only `local_test_gate` (line 212) which coordinare fills with `{enabled, timeout_seconds}` only (`dispatch_performer.py:1790-1793`). Same for `lint_command` (`baseline.py:36-37`). No escape hatch exists.
- B3 Test-path convention table misses whole ecosystems. `implementer/cycle.py:43-49` `_TEST_PATTERNS`: no `*.spec.ts`, `*.test.tsx`, `*Test.java`, `*Tests.cs`, Rust `tests/*.rs` or inline `#[cfg(test)]`. A tests turn writing one is reverted as `reverted_source` (`cycle.py:232-238`), red is never observed (`driver.py:355-357`), `MilestoneFailed` (`driver.py:488`). Also unanchored `test_.*` matches `greatest_common.py` (false positive revert of source).
- B4 Non-feature lanes demand an absolutely green suite. `implementer/driver.py:562` `if not regs and summary.passed:` in `_change` (also `_repair`, `_tests_lane`). The spec-089 pre-existing-red fix was applied to the gate only. One already-failing test anywhere = refactor/chore/dep-bump milestones fail after 3 attempts.
- B5 Docs are unconditionally out of scope. `implementer/cycle.py:194-200` reverts any docs/README/CHANGELOG path on every turn kind; `implementer/plan.py:43-44` raises `LaneNotForImplementer` for `work_kind in ("research","docs")`; the brief says "Do not create or edit documentation of any kind" (`backends/_card_docs.py:170-172`). Docs-only cards cannot be implemented by any lane.
- B6 TDD red/green is mandatory for every card without a `chore` brief, including no-brief cards (`implementer/plan.py:139-153` defaults lane to "feature"). Schema-first, CI/config-only, dependency bumps and one-line issues have nothing to fail, so they end in `MilestoneFailed` at `driver.py:488`.
- D  `performer/test_results.py:54-61` failure markers know pytest/mocha/rspec/maven/minitest only; Go/cargo/dotnet failures containing "connection refused" are classed `env` and the card is held.
- D  `driver.py:179` `test_conventions` placeholder always renders empty (runner_kind is "" on every path in `baseline.py`).
- D  Personas use Python vocabulary (`personas.py:45,75,77` ImportError / try/except).
- D  `__init__.py:189-191` resume base branch candidates are `main` only when `base_branch` unset; `master`/`develop` repos get no resume.
- D  `ci.py:135-136` "no progress" compares check-name sets not outcomes; a flaky job failing twice ends the run.
- D  `quality.py:60-65` lint output run through the env-signature matcher without the test-failure short circuit.
- D  `plan.py:194` single-turn scope joined with spaces but `resume.scope_segments` splits on `,`/`;`.
- Agnostic (keep): `observe.py` judge personas; `baseline.run_tests`; `scoping.py`; `commits.py`; `budgets.py`; `ci.classify_check_runs`; `resume.py` rules; `project_shape.py` (not used by the implementer).

## 2. QA — verified

- B1 App boots only when a flow/visual check is planned (`qa/__init__.py:232` `if plan.needs_baseline()`; `models.py:109-111`). Command checks such as `curl` against a JSON endpoint run with the app down and FAIL the criterion as a code defect.
- B2 A command check with `command: null` runs `true` and passes (`qa/execute.py:94-96`); a flow with zero steps passes (`execute.py:102-107`). Both satisfy the coordinare evidence floor with a fake executed check.
- B3 Plan-to-criterion binding is exact string equality (`qa/plan.py:126-130`) while the judge normalises (`judge.py:31,56,61`); any drift drops all checks, `EmptyPlan`, hard FAIL with one unmet criterion each.
- B4 The workflow never produces visual evidence (`report.py:52` gets `visual_evidence=None`; no `capture_screenshot` call in `workflows/qa/`), yet sets `visual_validation_required` for any UI plan; `qa_verdict.py:119-120` then returns `missing_visual_evidence` (bounce). The only rescue is `qa_capture.infer_app_start_command` which knows Rails/Django/Node only.
- B5 Boot proof is `curl -fsS` against `/` (`qa/boot.py:346`); APIs with no root route, gRPC/websocket servers, are "running but not answering HTTP" and HOLD as env-blocked.
- B6 Post-change surface read has no try (`qa/__init__.py:316`) while `dom.read_dom` raises on `networkidle` timeout (`dom.py:56-60`); any dev server holding a connection (HMR, ActionCable, SSE) crashes the run to `state=error`.
- B7 No-criteria cards either HOLD as `qa_env_blocked` or pass vacuously depending on what the planner emits (`__init__.py:220-222`, `qa_postprocess.py:292-308`).
- B8 The workflow's own `passed` is never read coordinare-side (`qa_postprocess.py:262-264` recomputes from `failures` only); regressions detected by `judge.py:153` and missing-baseline fail-closed (`__init__.py:349-353`) are dropped, so they report `qa_passed`.
- D  `_QA_ENV_FAILURE_PATTERNS` (`qa_postprocess.py:557-574`) reclassify real defects ("no such file", "connection refused", "not installed") as advisory env limits.
- D  DOM reader sees `h1,h2,h3,input,select,button,a,[role=alert]` only (`dom.py:20`); CSS/table/chart/img/custom-element changes are invisible and read as "nothing regressed".
- D  Legacy persona path infers visual requirement from keywords `view`, `render`, `component`, `screen` (`main.py:64-80`).
- D  Planned command checks do not inherit `workflow_env` (inferred; `adapter.py:60-69` vs `workspace.py:1720-1727`).
- D  Baseline worktree gets no dependency install or build (`baseline.py:48-51`, `boot.py:236-248`); compiled stacks and monorepos cannot boot the base.
- D  One `PORT`/one start command; monorepo frontend+backend cannot both boot (`boot.py:209-216`).
- D  `FlowAction` cannot assert status codes or JSON (`models.py:21`).
- Agnostic (keep): `project_shape.py`; boot override layering (`boot.py:77-123`); `ProjectShapeUnknown` fail-loud; shell-injection boundary (`boot.py:188-200`); evidence floor (`judge.py:63-84`, `qa_verdict.py`); base-ref resolution; boot proof as executed check.

## 3. Advocate + Curator — verified

- B1 Advocate comments even when labelling failed (`advocate/act.py:90-99`); `add_labels` raises 404 for a label that does not exist and never creates it (`performer/github.py:748`). Any repo without pre-created labels gets the same automated comment every cycle.
- B2 Curator livelocks on the newest N non-qualifying issues (`curator/candidates.py:36-37` breaks at `max_per_run`; skipped judgements apply no label). Issue N+1 onward is never evaluated.
- B3 Curator board dedup is dead in production (`curator/__init__.py:132` `hasattr(board, "on_board_ids")`; only the test fake defines it).
- B4 `backlog_column` is never applied; `add_item_to_project` only adds (`github.py:762-770`), so GitHub's default "item added -> Todo" automation puts curated items in a dispatch column.
- B5 Curator has no sensitive-issue triage and republishes an untrusted quote and free model prose publicly (`curator/__init__.py:209-214`); `reason` is ungated, multi-line quotes escape the blockquote.
- B6 Advocate grounding gate accepts an answer with zero prose citations if metadata lists one read file (`advocate/gate.py:92-102`).
- B7 Sensitive-keyword triage is English substring only (`advocate/settings.py:15-18`, `triage.py:18-22`).
- B8 Issue text interpolated into the classify prompt undelimited (`advocate/personas.py:49-55`); id check only verifies the id was sent (`gate.py:116`), so a forged `### issue_id:` block can steer an answer onto another issue in the batch (model susceptibility inferred; absence of structural defence verified).
- D  Doc sources must be files; a directory yields `read=False` for everything and every issue escalates (`advocate/docs.py:46-50`). Empty README counts as documentation. Citation match is exact path (`gate.py:53,71`); `_PATH_LIKE` extension list omits `.mdx/.adoc/Dockerfile/Makefile/.cs/.kt/.tf`.
- D  Bug reports labelled handled with no comment and no confidence floor (`advocate/__init__.py:238-243`, `gate.py:126-127`).
- D  No duplicate detection in either lane; no stale/escalated logic in the curator (`candidates.py:11-37`, `github.list_open_issues` fetches no dates); listing capped at 250 newest (`github.py:614-643`).
- D  Curator single un-batched call, 3000-token budget, whole run blocked on any failure (`curator/__init__.py:149-158`; `budget.py:93`).
- D  `_DISPATCH_COLUMNS` (`src/coordinare/config.py:733`) misses GitHub's real default `Ready`.
- C  English-only templates appended to any-language answers; empty-string support_channel_url substituted when unset (the literal survives only an unknown/malformed placeholder, `act.py:58-63`); `authorAssociation` never checked.
- Agnostic (keep): path-traversal refusal (`docs.py:42-45`); persona/evidence separation; label-based idempotency; code-decides-action schemas; per-batch failure tolerance; env-configurable settings; write-free proof.

## 4. Assessor + Architect — verified

- B1 Assessor has no verdict for "not work" or "epic, split it" (`assessor/models.py:77-86`); `force_ready` (`gate.py:110-115`) forces `ready=True` after two rounds with invented criteria. Questions and epics become built cards.
- B2 Ready with no criteria raises `GateError` (`gate.py:170-171`), uncaught in `assessor/__init__.py`: a performer error on exactly the docs/config/dep-bump/one-line cards where a model returns `criteria=[]`.
- B3 `Blueprint` requires >=1 milestone and >=1 criterion with `kind in {functional, visual, command}` (`architect/models.py:55,68,75`); refactor/docs/CI/dep-bump/flaky-test cards must fabricate an observation or fail schema twice (`SchemaViolation`).
- B4 No size guard on the committed plan (issue #396): the prose architect path commits backend output verbatim (`performer/main.py:1993-1995`); `size.py` is a small/large classifier, not a bound; workflow path bounded only by per-field `max_length` (~40 KB worst case, inferred).
- D  Survey allow-list refuses `$ < > \`` anywhere (`architect/allowlist.py:25`), so regex end-anchors, generics `List<T>`, `<div` are refused and still consume the 12-command budget (`survey.py:100`); `git grep`, `awk`, `jq`, `xargs`, `tree`, `stat`, `diff` not allowed (`allowlist.py:16-20`).
- D  Question dedup uses `min(|A|,|B|)` denominator (`_text.py:50-54`): short new questions swallowed by long answered ones; tokenisation is whitespace + ASCII punctuation (`_text.py:26-27`), so CJK never matches and re-asks until force_ready.
- D  `size_of` ignores module and criteria counts (`size.py:16-23`): a 12-module refactor is `small` (single turn); any column add is `large`.
- D  Closed vocabularies `DataModelChange.kind` / `Interface.kind` (`models.py:34,45`): no protobuf, GraphQL, component, Terraform resource, trait; omission flips size to small.
- D  Intake reads `AGENTS.md`/`CLAUDE.md` and `docs/cards` at repo root only (`architect/intake.py:12-13,118,130`); survey rooted at repo root (`survey.py:94-95`). Monorepos get the wrong context.
- D  Write-free proof fails on any pre-existing tree dirt (`architect/report.py:21,34-35,51-54`) (trigger inferred).
- D  No "cited paths must exist" rule anywhere (`architect/models.py:29` free text; nothing validates `Module.path`/`Milestone.scope`).
- D  Prose architect persona is Rails-shaped: `db/migrate/`, `spec/models/`, "schema before models before specs", floor of 3 milestones (`persona_service.py:182-194`). Active for `workflow: None`.
- C  Clarifications asked for any archetype; criteria persona names web surfaces first; over-budget survey commands dropped silently; `criteria_source == "card"` yields empty `Assessment.criteria`.
- Agnostic (keep): no AC formatting parsed; `project_shape.py` (not imported by these lanes); allow-list is program-based not path-based; survey persona names no language; blueprint persona scales down (schema floor is the contradiction); assessor structurally write-free; per-step budgets.

## 5. Implementer (CI, quality, resume) — verified

- B1 Zero check runs reads as green. `implementer/ci.py:63-69` returns `"pass"` when neither failed nor pending; `run_ci_phase` polls immediately after push (`__init__.py:349-352`, `ci.py:79`), when GitHub's check-runs list is normally empty. Also permanent false green for repos with no CI and for CI reporting via commit Statuses (CircleCI/Jenkins/Buildkite); `github.py:107-116` reads check-runs only.
- B2 `git commit` runs with hooks and raises `RuntimeError` (`implementer/commits.py:243-248`), caught nowhere (`driver.py:663` catches `MilestoneFailed` only). husky/pre-commit/commitlint repos (the `wip(#N):` salvage prefix is not a conventional type) crash the workflow instead of `partial_progress`.
- D  Test output reaching the observer is the stdout tail only, capped at 2000 chars (`workspace.py:1635`, `adapter.py:65-67`, `models.py:43`); runners that summarise on stderr (Gradle, Maven, Go vet, cargo) hand the observer nothing, which becomes `InfrastructureBlocked` or a false `all_passed`.
- D  Quality gate silently skips when no lint rule exists for the stack (`quality.py:109-111`); `npm run lint` with no `node_modules` is treated as a lint defect and burns repair turns.
- D  Check-runs unpaginated at 100 (`github.py:113`); 30-minute CI ceiling default (`budgets.py:38`); `cancelled`/`stale` treated as failures (`ci.py:46`) so `cancel-in-progress` concurrency groups burn repair budget.
- D  `InfrastructureBlocked` patterns are GitHub-Actions-and-Docker specific (`infrastructure.py:6-20`).
- C  `_forbidden_paths` is dead (never delivered to backends, `adapter.py:224-228`).

## 6. Reviewer + Security — verified

- B1 Empty or unparseable diff = vacuous pass in both lanes. `security/scanner.py:96-97` returns `([], [])` for no files; `full_coverage([])` is True; reviewer `gate.py:148-152` returns `approved`. Reachable: the coordinare sanitizer drops binary and vendored sections before injection (`dispatch_performer.py:483-489`), so binary-only diffs arrive empty (lockfile sections are not filtered by `_DIFF_NOISE_PATH_MARKERS` and arrive as parsed changed files).
- B2 Docs-only, CI/config-only, lockfile-only diffs = security `env_blocked` every time (`scanner.py:100-101` no applicable tools; `:134-140` examined none). Fail-closed is indiscriminate: a README typo can never get a verdict.
- B3 Non-Python/JS stacks depend on the model guessing installed tools (`security/tooling.py:115-118`); the image has bandit/semgrep/ruff/eslint/shellcheck only (`Dockerfile.full:22-40`). gosec/cargo-audit/brakeman/gitleaks/trivy absent, so Go/Rust/Java/C#/Ruby/Terraform hold unless semgrep is picked (semgrep `--config auto` also needs egress).
- B4 Scanner findings bypass the anchor rule and the scan is not scoped to the diff (`security/gate.py:172-181`; `scanner.py:108` uses model-authored argv verbatim), so pre-existing repo findings fail the PR; whole-repo scans time out at 120 s on large repos.
- B5 Scanner evidence sliced to 500 (`security/gate.py:109`) into a `max_length=200` field (`models.py:62`): `ValidationError` uncaught at `security/__init__.py:198` on minified JS, generated code, long lines.
- B6 Files beyond the 60 000-char diff truncation are invisible, not "unread" (`reviewer/intake.py:84-87` marks only the last file); coverage passes on the visible subset and `approved` can be posted for a PR mostly unseen.
- B7 Coordinare-side floor is a static semgrep + bandit pair (`src/coordinare/services/security_scanner.py:77-78`, called from `dispatch_performer.py:411`); vacuous pass on every repo where both scanners report nothing (the defect #366 fixed only on the performer side).
- D  No dependency/IaC/config vulnerability category (`security/models.py:22-46`); everything unknown maps to `other_insecure_pattern` = medium = advisory.
- D  Reviewer has no severity tier: any finding is `changes_requested` (`reviewer/gate.py:148-152`), with `style` and `test_missing` in the default categories (`models.py:37-45`); docs paths trip `add_documentation_by_implementer` (`gate.py:123-138`, root-anchored `docs/`, `doc/`, README/CONTRIBUTING/CHANGELOG).
- D  Root-level unchanged files can never anchor a security finding (`security/__init__.py:269` requires `/` in the path); two contradictory "opened" definitions (`reviewer/survey.py:66-70` vs `117-131`); rename/mode/delete-only diffs count as fully covered with nothing read (`diffparse.py:57-76,99-104`); paths with spaces mis-parsed (`diffparse.py:46-53`); `./`-only path normalisation (`security/gate.py:60-65`); `introduced_by` exact-match (`gate.py:55-56`); `OK_EXIT_CODES={0,1}` and empty-output = unavailable (`scanner.py:49,68-69`); free-form scanner `line` values crash (`gate.py:106`, inferred).
- Agnostic (keep): allow-list is program-based; severity by category in code; no CWE table in the performer; model reads tool output with no format assumption; per-file coverage as abstention primitive; finding schema is unified-diff concepts; personas name no framework.

## 7. Documenter + Closer — verified

- B1 Wiki is one hardcoded directory (`documenter/inventory.py:190` `docs/wiki`; every write target in `plan.py`). mkdocs/Sphinx/docusaurus/monorepo docs are never enumerated; update mode returns `docs_committed` having read and written nothing; init mode creates a second competing docs tree.
- B2 Pages without Diátaxis `kind:` frontmatter (any pre-existing wiki) are always dropped after a paid model call (`gate.py` kind checks; `plan.py:159-169` `kind=None`).
- B3 Decision pages must live in `docs/wiki/decisions/` (`gate.py:134`); established ADR conventions are dropped at the gate.
- B4 "Cited paths must exist" promotes bare backticked filenames and checks them at repo root only (`gate.py:130-131`, `inventory.py:30`); monorepo pages naming `package.json` or `settings.py` are dropped whole, silently, with the run still `docs_committed`.
- B5 `../` links are `Path.resolve()`d to absolute filesystem paths (`inventory.py:141-144`) and therefore never resolve; any page linking out of the wiki is dropped.
- B6 Closer posts the closing review before resolving the threads it announces (`closer/__init__.py:136` then `:146-161`; `post.py:41`); a failed resolve leaves a permanent "APPROVED / every thread resolved" review on a PR whose threads are still open, exactly on repos requiring conversation resolution.
- B7 Closer fetches `reviewThreads` only (`github.py:469`), never `reviews { state }` or `reviewDecision`; a human "Request changes" with no inline thread, or zero threads, yields `approved` and "Handing off for human review".
- B8 An `addressed` judgement is traceable to any comment in the thread including the author's own disagreement (`closer/gate.py:29-39`); the human reviewer's thread gets resolved on the author's say-so.
- D  No bot/human distinction anywhere in the closer (`classify.py`, `models.py:19`); Copilot-only reviews block forever or get approved on the bot's word.
- D  Pointer files `AGENTS.md`/`CLAUDE.md` are created, not just refreshed (`documenter/__init__.py:205-216`); `documenting_side_run=None` handled oppositely by two guards (inferred reachability).
- D  Non-wiki doc pages are written and committed but never indexed (`__init__.py:264-265`); page selection is pure citation intersection with no "does this change warrant docs" judgement (`plan.py:131-169`), so refactors produce up to 7 pages of churn and CI-only changes produce nothing; init evidence takes root files alphabetically so dotfiles win (`__init__.py:305,322`); nested `source_dirs` dropped (`inventory.py:266-269`); "has tests" means top-level `tests/` or `test/` (`inventory.py:261`), so Go/Jest/RSpec repos get zero package pages; `stray_paths` revert is documented but has no caller (`commit.py`); `project_shape` tree is a sorted 400-path prefix (`project_shape.py:40`); `closer/models.py:91` bakes a coordinare-repo spec path.
- C  Any heading containing `#\d+` is a changelog heading (`gate.py:50-52`); `DOC_PLAN_CAP=1` defers every real page; `readme_shape_ok` is dead.
- Fixture gap: closer fixtures have no zero-thread, bot-author or author-disagreement case; documenter fixtures are one Python repo with `docs/wiki`.

## 8. Shared plumbing — verified

- B1 `project_shape` sees only the alphabetically last ~2000 chars of `git ls-files`. Chain: `workspace.py:1635` `_MAX_OUTPUT = 2000` with `truncate="tail"` (`adapter.py:65`), then `models.py:43` `output_excerpt=output[:2000]`, consumed at `project_shape.py:181`. `MAX_TREE_PATHS=400` is unreachable, the prompt's `len(tree)` is wrong, and root manifests (`Cargo.toml`, `Gemfile`, `go.mod`, `package.json`, `pyproject.toml`) sort before the tail and are dropped, so the model usually gets "(no readable root files)". Every lane that calls project_shape (QA, documenter) is starved. This is the single most leveraged defect in the audit: the one genuinely stack-agnostic component is fed a truncated view.
- B2 Only postgres and redis get coordinare-managed init recipes (`service_inference/schema.py:39,162`); a generic kind is hostable when its binary already resolves on PATH (`agent.py:253-287`), but nothing ships mysqld/mongod/rabbitmq/elasticsearch, so for those the manifest is rejected or pushed to `external_required`, which validates only its required env vars (`services-start.sh.j2:65-73`) rather than installing anything.
- B3 Generic service launch line defaults to the redis shape when `start_args` is unset (`services-start.sh.j2:179`; `start_args` argv-style override is the escape hatch, `schema.py:153-160`) and records `$!` as started unconditionally; only the port probe later catches a daemon that died on a bad flag, and the cause is reported as an env-cache health failure.
- D  Post-activation toolchain verification asserts Ruby only (`workspace.py:356` `_TOOLCHAIN_ADVERTISEMENTS`); readiness probing is `pg_isready` for postgres and a bare TCP connect for everything else (`services-health.sh.j2`); timeouts sized from postgres (`workspace.py:710` 300 s; `devenv-profile.sh:168` 100 s); default command timeout 300 s and test run 600 s with 2000-char output cap (`toolkit.py:83`, `baseline.py:104`) are thin for Gradle/Maven/cargo cold builds; image tool inventory is Python + Node only (`Dockerfile.base:5-16`, `Dockerfile.full:22-40`), the Rails baseline itself comes only through the env cache; `devenv-profile.sh:98-115` publishes `.deb` payloads only; `noise_paths.py:37` knows Ruby/Node/Python artifacts and `BUILD_VCS_NOISE` has no performer reader; `schema_guard.extract_json` spans first `{` to last `}` over promoted reasoning (`schema_guard.py:35`, `budget.py:153`).
- Agnostic (keep): `ProjectShape` free text + `shape_persona` + `_manifest_excerpts` (design right, input starved); `ProjectShapeUnknown`; exit-code-only verdicts (`toolkit.py:78-97`); `bash -c` with `BASH_ENV` (`workspace.py:1638-1662`); UTF-8 replace decoding; token budgets not wall-clock; schema_guard single reprompt; `run_service_readiness` no-op with no services; profile never `set -e`/`exit`s; `path_has_agent_config` segment matching.

## 9. Coordinare-side inputs — verified

- B1 `coordinare_ci_detection` knows Ruby/Python/Node/Make, root-only, first match wins (`__init__.py:197`); Ruby first with `.rubocop.yml` hardcoding `bundle exec rspec` (minitest apps get 127); Python is a `"pytest" in content` substring over `pyproject.toml` only (`:113-122`; Django/tox/nox/setup.py mis- or undetected); Node emits bare `npm test` regardless of pnpm/yarn/bun (`:152-153`); Makefile with both `ci:` and `test:` sets `test_command=None` (`:169-178`).
- B2 `LocalTestGateConfig` is `extra="forbid"` with `enabled/timeout_seconds/max_fix_attempts` only (`src/coordinare/config.py:1559-1572`); no `test_command`/`lint_command` knob reaches the performer's (dead) overrides. Operators cannot override detection.
- B3 The 089 gate treats a detected-but-uninstalled runner (exit 127) as a code defect: no 127/"command not found" entry in `infrastructure.py:6-17` or `_ENV_FAILURE_SIGNATURES`, deliberately (`main.py:193`); the 167 lane guards it (`baseline.py:30`), the 089 gate does not.
- B4 Only postgres/redis service kinds (`env_manifest.py:160-163`), contradicting the inference prompt's own examples (`service_inference/prompt.py:45-47` lists mysql, elasticsearch, minio, rabbitmq, mongodb, memcached, kafka, localstack).
- B5 CI provider is never detected; zero mentions of gitlab/circleci/buildkite/jenkins in `src/coordinare` or `packages`; `ci_gate.py:18` and `failure_classification.py:71-81` are GitHub check semantics; non-GitHub CI classifies `unknown` forever.
- D  `test_results.py:54-63` failure markers cover pytest/mocha/rspec/maven/minitest only (Go, cargo, dotnet, vitest unmatched: env misclassification and double full-suite runs); coordinare lint gate runs `make ci` under a 60 s budget (`monitor_performer.py:1294-1307`, fails open); `STRUCTURED_SPEC_FILES` has no go.mod/Cargo.toml/pom.xml/gradle/csproj/pyproject/requirements (`env_manifest.py:24-32`) so non-Ruby/Node/Python manifests are empty; `_RUNTIME_ACTIVATION` knows ruby/node/python only (`:466-470`); hard verification covers runtimes and gems while system/node-package checks are soft (`:245-287`); QA persona asserts postgres and redis are running and prescribes `rails db:migrate` (`persona_service.py:533-544`); `ProjectConfiguration` override merge is shallow (`config.py:1859`) so a second symphony must re-declare the entire `performers` block to change one QA env var; no default `path_classes` taxonomy (`persona_classifier.py:463-465`; `config.yaml` ships a Rails one).
- C  Persona lint list is Ruby-first (`persona_service.py:48`); output schema example uses `.rb` and port 3000 (`:584,589`); "never modify `.github/workflows/`" protects nothing on GitLab (`:280-284`).
- Agnostic (keep): `failure_signature.py` drift normalisation; `failure_classification` decision table (GitHub-coupled, not language-coupled); `ci_gate.compare_signatures`; `persona_classifier` fnmatch over operator globs with a language-free prompt; `env_manifest_llm` generic system-package prompt; `service_inference/prompt.py` polyglot manifest list; validator start/health/stop dry run; `shq` quoting; `*_HOST` loopback aliasing.

## Cross-lane summary

Counts (verified at the cited line unless marked inferred): 51 blockers, ~60 degraded, ~20 cosmetic across 10 lanes plus plumbing.

**Where the Rails shape actually lives.** Not in persona prose (the census found 7 `rspec`, 6 `rails` mentions across 16k lines). It lives in five structural places:
1. Test discovery and parsing: `coordinare_ci_detection` (4 stacks, root only), `_TEST_PATTERNS`, `test_results` markers, dead override fields, `LocalTestGateConfig` forbid.
2. Milestone shape: mandatory red/green for every non-`chore` card; docs out of scope for all lanes; `Blueprint` cannot be empty; assessor cannot say "not work".
3. Services: postgres/redis as the only coordinare-managed kinds, generic launch defaults redis-shaped unless `start_args` is supplied, `pg_isready` special case, postgres-sized timeouts.
4. Verdict plumbing: QA `passed` never read, null command passes, boot gated on browser checks, security empty-diff pass, reviewer no severity tier, zero check runs = green, closer posts before resolving.
5. Input starvation of the one agnostic component: `project_shape` fed a 2000-char tail of `git ls-files`.

**Correctness defects that are not about generality but were found in passing and would bite Rails too:** QA null-command pass, QA `passed` discarded, QA exact-equality plan filter, security evidence 500-into-200 crash, zero-check-runs green, `git commit` RuntimeError uncaught, closer post-before-resolve, advocate comment-after-label-failure, curator livelock, curator `on_board_ids` dead, `plan.py:194` scope join, `_text.token_overlap` min-denominator, `../` link resolve.

## Recommended fix order (by leverage, each a spec)

1. **Shared output plumbing** (unblocks QA, documenter, project_shape, observer): raise/route `_MAX_OUTPUT` per call (`git ls-files` and test runs need head+tail or a file), include stderr, fix `models.py:43` head slice. One small PR, largest blast radius.
2. **Test command and test-path contract**: add `test_command`/`lint_command`/`test_paths` to `LocalTestGateConfig` and `Score`; make `_TEST_PATTERNS` extensible via `workflow_env`/shape; add Go/Java/.NET/Rust/pyproject-less Python detectors and subdir probing; make `test_results` markers a fallback behind the model observer; 127 = env in the 089 gate.
3. **Milestone shape**: `chore`/`docs`/`config` lanes that skip red/green; assessor `kind` field with `not_work`/`needs_split`; `Blueprint` allow zero criteria for chore kinds; implementer docs allowed when the brief is docs; `_change` uses baseline comparison.
4. **QA verdict integrity**: read the workflow `passed`; reject null commands and empty flows at schema; normalise plan criterion binding; boot when any check needs a URL; health path configurable; wrap the post-change observe; produce screenshots or stop declaring visual required.
5. **Reviewer/security verdicts**: empty-diff = explicit "nothing to review" verdict not pass; unscannable archetypes = "not applicable" not env_blocked; scope scanner argv to changed files; fix evidence length; severity tier in the reviewer; remove the bandit-only coordinare floor or make it language-aware.
6. **Services**: extend kinds (mysql, mongo, rabbitmq, elasticsearch, memcached) with per-kind launch/health templates; make the generic launch default language-aware instead of redis-shaped.
7. **Closer and CI truth**: fetch `reviewDecision`/review states and author type; resolve before posting; require the acquiescing comment to come from the raiser; zero check runs = pending with a floor wait; read commit Statuses too.
8. **Documenter**: docs root from `project_shape`/config, not `docs/wiki`; accept missing `kind` frontmatter; fix `../` resolution; scope citation existence to the page's package.
9. **Advocate/curator**: create labels if missing or refuse to comment; curator label skipped issues or paginate past them; implement `on_board_ids`; set the column after add; sensitive triage in the curator; delimit issue text in prompts.
10. **Architect survey**: allow `$`, `<`, `>` inside quoted arguments; allow `git grep`, `awk`, `jq`; plan size cap on the prose path (#396).
