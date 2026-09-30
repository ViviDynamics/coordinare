# Shared functions for Vivi Dynamics skill scripts. Source, do not execute.
# bash 3.2 compatible. Dependencies: gh, jq, git.

coordinare_die() { local code=$1; shift; printf '{"error":%s}\n' "$(printf '%s' "$*" | jq -Rs .)"; echo "error: $*" >&2; exit "$code"; }

coordinare_root() {
  git rev-parse --show-toplevel 2>/dev/null || coordinare_die 2 "not inside a git repository"
}

# COORDINARE_ASSIGNEE is per person, so a value already in the environment (for
# example from an uncommitted .claude/settings.local.json) beats repo.env.
coordinare_load_env() {
  local root file line key val env_assignee=${COORDINARE_ASSIGNEE:-}
  root=$(coordinare_root) || exit 2
  file="$root/repo.env"
  [ -f "$file" ] || coordinare_die 2 "repo.env not found at $file (see ADOPTING.md)"
  while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in COORDINARE_*=*) ;; *) continue;; esac
    key=${line%%=*}; val=${line#*=}
    # Drop an inline comment: whitespace followed by # and the rest of the line.
    case "$val" in *[[:space:]]#*) val=${val%%[[:space:]]#*};; esac
    while :; do case "$val" in *[[:space:]]) val=${val%?};; *) break;; esac; done
    val=${val%\"}; val=${val#\"}
    val=${val//\$HOME/$HOME}
    export "$key=$val"
  done < "$file"
  if [ -n "$env_assignee" ]; then export COORDINARE_ASSIGNEE="$env_assignee"; fi
  export COORDINARE_ROOT="$root"
}

coordinare_require() {
  local k
  for k in "$@"; do
    eval "[ -n \"\${$k:-}\" ]" || coordinare_die 2 "repo.env is missing required key $k"
  done
}

# Prints the token file path to use, or nothing (exit 0) when there is no
# token file but GH_TOKEN/GITHUB_TOKEN is set in the environment, e.g. under
# CI, or when gh has auth configured (gh auth status succeeds). Dies with
# exit 2 only when no file, no env token, and gh has no auth.
coordinare_token_file() {
  local org
  if [ -n "${COORDINARE_GH_TOKEN_FILE:-}" ] && [ -f "$COORDINARE_GH_TOKEN_FILE" ]; then echo "$COORDINARE_GH_TOKEN_FILE"; return; fi
  if [ -f "$HOME/.gh_token" ]; then echo "$HOME/.gh_token"; return; fi
  org=${COORDINARE_REPO%%/*}
  if [ -n "$org" ] && [ -f "$HOME/.gh_token" ]; then echo "$HOME/.gh_token"; return; fi
  if [ -n "${GH_TOKEN:-}" ] || [ -n "${GITHUB_TOKEN:-}" ]; then echo ""; return; fi
  if gh auth status >/dev/null 2>&1; then echo ""; return; fi
  coordinare_die 2 "no GitHub token file found (COORDINARE_GH_TOKEN_FILE, ~/.gh_token, ~/Workspaces/<org>/.gh_token), no GH_TOKEN/GITHUB_TOKEN in the environment, and gh has no configured auth"
}

# COORDINARE_SLEEP is a test-only override: the test harness sets it to 0 so poll
# loops run instantly. Production never sets it.
coordinare_sleep() { sleep "${COORDINARE_SLEEP-$1}"; }

coordinare_gh() {
  # stdout and stderr are captured separately: gh's stderr must never be folded
  # into the JSON a caller parses. Transient classification looks at both, since
  # gh writes some diagnostics to stdout.
  local token_file token max=${COORDINARE_RETRY_MAX:-5} attempt=1 out err code errf
  token_file=$(coordinare_token_file) || return 2
  if [ -n "$token_file" ]; then
    token=$(cat "$token_file" 2>/dev/null) || { echo "error: cannot read token file $token_file" >&2; return 2; }
  else
    token=${GH_TOKEN:-${GITHUB_TOKEN:-}}
  fi
  errf=$(mktemp) || { echo "error: cannot create a temp file" >&2; return 2; }
  while :; do
    code=0
    if [ -n "$token" ]; then
      out=$(GH_TOKEN="$token" gh "$@" 2>"$errf") || code=$?
    else
      out=$(gh "$@" 2>"$errf") || code=$?
    fi
    err=$(cat "$errf" 2>/dev/null) || err=""
    if [ "$code" -eq 0 ]; then
      rm -f "$errf"
      if [ -n "$err" ]; then printf '%s\n' "$err" >&2; fi
      printf '%s\n' "$out"
      return 0
    fi
    if printf '%s\n%s' "$err" "$out" | grep -qiE 'rate limit|HTTP 50[234]|timeout|timed out|connection reset|Could not resolve'; then
      if [ "$attempt" -ge "$max" ]; then
        rm -f "$errf"
        echo "gh: transient failure after $max attempts: $*" >&2
        if [ -n "$err" ]; then printf '%s\n' "$err" >&2; fi
        if [ -n "$out" ]; then printf '%s\n' "$out" >&2; fi
        return 3
      fi
      echo "gh: transient error, retry $attempt/$max: $*" >&2
      coordinare_sleep $((attempt * 5)); attempt=$((attempt+1)); continue
    fi
    rm -f "$errf"
    if [ -n "$out" ]; then printf '%s\n' "$out" >&2; fi
    if [ -n "$err" ]; then printf '%s\n' "$err" >&2; fi
    return "$code"
  done
}
