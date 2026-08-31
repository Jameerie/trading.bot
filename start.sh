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

# Read .env without sourcing it. A .env written by trading-bot.bat has CRLF
# line endings, and sourcing that leaves a carriage return inside the value —
# a correct token that gets refused with a 401, which is near impossible to
# diagnose from the outside. Strip it here instead.
load_env() {
    [ -f .env ] || return 0
    while IFS='=' read -r key value || [ -n "${key:-}" ]; do
        case "$key" in
            TRADING_BOT_TOKEN|TRADING_BOT_API_KEY)
                export "$key=${value%$'\r'}" ;;
        esac
    done < .env
}
load_env

# 0.0.0.0 so a phone on the same wifi can reach it. The token is what keeps it
# private; SETUP.md section 4 explains the trade-off before you expose it wider.
exec "$VPY" -m trading_bot --config config/default.toml serve --host 0.0.0.0 "$@"
