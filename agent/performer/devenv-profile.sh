# Auto-source any mounted env-cache activate.sh.
#
# Agent CLI tool runners (codex, claude, opencode, junie) spawn shell
# tool calls in different modes:
#   - codex uses `bash -lc` (login)        → sources /etc/profile → /etc/profile.d/*
#   - others may use `bash -c` or `sh -c`  → only BASH_ENV / ENV is honored
# This script is installed both as /etc/profile.d/zz-devenv.sh AND referenced
# by BASH_ENV/ENV in the image, so every shell mode picks it up.
#
# Re-entry guard: bash sources BASH_ENV in EVERY non-interactive subshell —
# including command substitution like `$(rbenv init - bash)`. Without this
# guard, activate.sh recurses infinitely. Once we've sourced it for this
# process tree, _DEVENV_SOURCED is exported, and subshells skip the work.
if [ -n "${_DEVENV_SOURCED:-}" ]; then
  return 0 2>/dev/null || true
else
  export _DEVENV_SOURCED=1
  for _devenv_activate in /devenv/*/activate.sh; do
    [ -r "$_devenv_activate" ] && . "$_devenv_activate"
  done
  unset _devenv_activate
fi
