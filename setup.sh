#!/usr/bin/env bash
# Validate the Knows benchmark environment (credentials, auth, drive links).
# Usage: ./setup.sh [--headed] [--skip-mint] [--skip-link-check] [--skip-service-account]
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYBIN="${PYBIN:-/opt/miniconda3/envs/knows/bin/python}"

if [[ ! -x "$PYBIN" ]]; then
    echo "warning: $PYBIN not found; falling back to python3 on PATH" >&2
    PYBIN="python3"
fi

exec "$PYBIN" "$SCRIPT_DIR/scripts/setup_env.py" "$@"
