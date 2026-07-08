# Quickstart: verifying agent-config commit blocking (spec 131)

Two layers stop a performer from committing its tool-config dirs (`.codex`, `.claude`,
`.hermes`, `.junie`, `.opencode`, `.openclaw`, `.pi`, …) into a symphony repo.

## Layer 1 — prevention (never staged)

A performer clone gets the agent-noise globs written to `.git/info/exclude` (repo-local,
untracked). To reproduce in a scratch repo:

```bash
mkdir /tmp/ws && cd /tmp/ws && git init -q
printf '.codex/\n**/.codex/\n.claude/\n**/.claude/\n' >> .git/info/exclude   # (what the writer does)
mkdir .codex && echo junk > .codex/session.json
echo "real change" > app.py
git add -A && git status --porcelain      # → only app.py is staged; .codex/ is ignored
git -c user.email=x@y -c user.name=x commit -qm x
git show --stat HEAD                        # → app.py only; NO .codex/
cat .gitignore 2>/dev/null || echo "(repo .gitignore untouched)"
```

Legitimate dot-paths are NOT excluded:

```bash
mkdir .github && echo ci > .github/workflows.yml && echo ex > .env.example
git add -A && git status --porcelain      # → .github/… and .env.example ARE staged
```

## Layer 2 — guard (never merged, even after `git add -f`)

If an agent force-adds an ignored path, the commit/push guard strips it:

```bash
git add -f .codex/session.json            # force past the ignore
# … performer commit/push path runs the guard …
# → .codex/session.json is unstaged/dropped, real change proceeds,
#   and a `commit_guard.agent_artifact_stripped` event is logged (paths only)
```

## Verify the single source of truth

```bash
# The coordinare diff filter must stay consistent with the shared agent-dir set:
env -u COORDINARE_INFERENCE_MAX_TOKENS -u HERMES_CONTEXT_WINDOW \
  .venv/bin/pytest tests/unit/test_diff_noise_drift.py -q
# Performer-side prevention + guard + real-git behavior:
.venv/bin/pytest agent/performer/tests/test_workspace_agent_ignore.py \
                 agent/performer/tests/test_noise_paths.py -q
```

## What this fixes

The observed incident — a 573 KB PR diff that was 99% committed `.codex/` junk, which blew a
QA prompt past the model's context window and looped — cannot recur from newly created PRs:
the `.codex/` (and every other agent dir) is never staged, and the guard strips any that slip
through, so the PR diff injected into QA/review prompts stays clean.
