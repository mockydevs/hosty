#!/usr/bin/env bash
# Linux env for Claude Cowork sandbox sessions.
# The Windows .venv doesn't run in the sandbox, and venvs can't be installed
# onto the mounted folder (FUSE). So the Linux venv lives in the sandbox home.
#
# Usage (from backend/):
#   bash scripts/cowork-env.sh          # create/update ~/venvs/hosty
#   ~/venvs/hosty/bin/python -m pytest tests/ -q -p no:cacheprovider
#   ~/venvs/hosty/bin/ruff check app/ tests/
set -euo pipefail
export UV_LINK_MODE=copy
export UV_PROJECT_ENVIRONMENT="$HOME/venvs/hosty"
uv sync --group dev
echo "Linux venv ready at $HOME/venvs/hosty"
