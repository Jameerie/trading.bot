#!/usr/bin/env bash
#
# Start the trading.bot web app. Run ./setup.sh once first.
#
# Any argument is passed through to the server, so this works too:
#     ./start.sh --port 9000
#     ./start.sh --host 127.0.0.1     (this machine only)

set -euo pipefail
cd "$(dirname "$0")"

VPY=".venv/bin/python"
[ -x "$VPY" ] || VPY=".venv/Scripts/python.exe"
if [ ! -x "$VPY" ]; then
    printf '\033[31mNot set up yet.\033[0m Run ./setup.sh first.\n' >&2
    exit 1
fi

# Load the access token without echoing it into the shell history.
if [ -f .env ]; then
    set -a
    # shellcheck disable=SC1091
    . ./.env
    set +a
fi

# 0.0.0.0 so a phone on the same wifi can reach it. The token is what keeps it
# private; SETUP.md section 4 explains the trade-off before you expose it wider.
exec "$VPY" -m trading_bot --config config/default.toml serve --host 0.0.0.0 "$@"
