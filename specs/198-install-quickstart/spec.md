# Install and init Quick Start

Make the release installer and guided setup the default route to a running dashboard.
Keep checkout setup in CONTRIBUTING and link Kubernetes operators to the chart README.

The README must include prerequisites, the installer, an adjacent download and inspect
alternative, init prompts, startup choices, the dashboard URL and a security notice.
Supporting guides cover self-hosted models, Docker performers, user services, answers
files, upgrades, removal, storage locations and actual setup failure messages.

Check examples against install.sh, argparse definitions, init, presets, preflight,
doctor and service code. Write regression tests before editing documentation. Parse
commands extracted from guides with the real CLI parser, validate installer options
without installing, check local links and validate answers keys. Run existing installer,
setup, service and preflight tests plus lint, typecheck and the ruff ratchet.

Acceptance: a new operator needs no checkout, documented commands match shipped code,
and maintainers retain a working checkout workflow. Do not change runtime behavior,
workflows or dependencies. Leave changes uncommitted. Independently review the full diff
and write the PR body to /tmp/pr198.md.
