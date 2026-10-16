# Guided setup implementation plan

Goal: implement the supplied issue in the existing isolated 195-work worktree.

Architecture: init_cli owns argparse and orchestration; init_answers owns prompts
and answer precedence; init_probes owns detection and Projects listing;
init_files owns generation and safe persistence. user_config owns XDG paths and
loading the dedicated env file. Existing presets and preflight own expansion and
external validation. Use Python, argparse, PyYAML and existing httpx.

## Tasks

1. Write tests for CLI generation, interaction, overwrite safety and discovery.
   Run them red before adding implementation.
2. Implement user paths and secret persistence, minimal preset generation,
   detection, prompts and CLI dispatch. Run focused tests green.
3. Add failure and adversarial regression cases for malformed answers, shell
   metacharacters in credentials, partial files, stale exported values, and
   preserving advanced settings on repeat setup.
4. Run touched-file lint, mypy, the ruff ratchet and focused regressions. Review
   the complete diff and write /tmp/pr195.md. Leave all files uncommitted.

## Review focus

No terminal must never hang. Existing config or env files must not be lost.
Secrets must round trip literally and never appear in config or errors. Shell
commands shown to operators must quote paths. Installed wheels must work outside
a checkout. No new file may exceed the repository size limit.
