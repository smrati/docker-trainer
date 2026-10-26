#!/usr/bin/env bash
#
# Start the Docker Trainer.
#
#     ./start.sh                 # 8000, or the next free port
#     ./start.sh --reload        # restart when the code changes
#     ./start.sh --port 9000     # start looking from 9000
#     ./start.sh --host 0.0.0.0  # reachable from your phone
#
# Any argument is passed straight through to scripts/serve.py.

set -euo pipefail

# Run from the project root, whatever directory this was called from.
cd "$(dirname "$0")"

if command -v uv >/dev/null 2>&1; then
    # `uv run` creates the environment and installs what is missing, so a
    # fresh clone works with this one command.
    exec uv run python scripts/serve.py "$@"
fi

if [ -x .venv/bin/python ]; then
    exec .venv/bin/python scripts/serve.py "$@"
fi

echo "Neither uv nor a .venv was found." >&2
echo "Install uv (https://docs.astral.sh/uv/), then run ./start.sh again." >&2
exit 1
