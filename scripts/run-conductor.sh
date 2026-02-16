#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export PYTHONPATH="${ROOT_DIR}/src:${PYTHONPATH:-}"
export COORDINARE_RUN_MODE="shell"

if [[ $# -eq 0 ]]; then
  set -- --config "${ROOT_DIR}/config.yaml"
fi

exec uv run python -m coordinare "$@"
