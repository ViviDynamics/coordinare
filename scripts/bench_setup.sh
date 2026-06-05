#!/usr/bin/env bash
# 077: push the conductor-bench fixture branches and open the review-target PRs,
# then write the {role: pr_url} map persona_bench.py consumes. Idempotent-ish:
# re-running reuses existing PRs (gh pr create fails loudly if one exists; we
# fall back to `gh pr view`).
#
# Prereq: the bench repo must already exist remotely (the fine-grained PAT can't
# create repos — have an admin create an empty private ViviDynamics/conductor-bench
# first). Pass its clone URL as $1.
#
# Usage: scripts/bench_setup.sh <repo_git_url> [bench_local_dir]
set -euo pipefail
REPO_URL="${1:?usage: bench_setup.sh <repo_git_url> [bench_local_dir]}"
BENCH_DIR="${2:-$(cd "$(dirname "$0")/../.." && pwd)/conductor-bench}"
COORDINARE_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PRS_OUT="$COORDINARE_DIR/tmp/bench_prs.json"
export GH_TOKEN="$(cat "$COORDINARE_DIR/../.gh_token" 2>/dev/null || grep '^GH_TOKEN=' "$COORDINARE_DIR/.env" | cut -d= -f2)"

cd "$BENCH_DIR"
git remote get-url origin >/dev/null 2>&1 || git remote add origin "$REPO_URL"
git remote set-url origin "$REPO_URL"
echo "Pushing branches to $REPO_URL ..."
git push -u origin main
for b in fix/reviewer-offbyone feat/security-sqli feat/qa-behavior feat/techwriter-nodoc feat/impl-failing-test feat/closer-ready; do
  git push -u origin "$b"
done

# role -> head branch for the four review-target PRs (bash 3.2 safe: no assoc arrays)
PR_PAIRS="reviewer:fix/reviewer-offbyone security:feat/security-sqli qa:feat/qa-behavior closer:feat/closer-ready"
mkdir -p "$COORDINARE_DIR/tmp"
echo "{" > "$PRS_OUT"; first=1
for pair in $PR_PAIRS; do
  role="${pair%%:*}"; br="${pair#*:}"
  url="$(gh pr view "$br" --json url -q .url 2>/dev/null || true)"
  if [ -z "$url" ]; then
    url="$(gh pr create --base main --head "$br" \
        --title "bench($role): $br" \
        --body "conductor-bench fixture PR for the $role persona benchmark. Do not merge." \
        2>/dev/null | tail -1)"
  fi
  echo "  $role -> $url"
  [ $first -eq 0 ] && echo "," >> "$PRS_OUT"; first=0
  printf '  "%s": "%s"' "$role" "$url" >> "$PRS_OUT"
done
echo "" >> "$PRS_OUT"; echo "}" >> "$PRS_OUT"
echo "wrote $PRS_OUT"
cat "$PRS_OUT"
