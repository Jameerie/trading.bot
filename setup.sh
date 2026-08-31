#!/usr/bin/env bash
#
# One-time setup for trading.bot.
#
# Run this once:   ./setup.sh
# Then start it:   ./start.sh
#
# It is safe to run again — an existing environment is reused and an existing
# access token is never rotated out from under you.

set -euo pipefail
cd "$(dirname "$0")"

VENV=".venv"
MIN_MINOR=11

say()  { printf '\n\033[1m%s\033[0m\n' "$*"; }
ok()   { printf '  \033[32mok\033[0m  %s\n' "$*"; }
warn() { printf '  \033[33m!!\033[0m  %s\n' "$*"; }
die()  { printf '\n\033[31mSetup stopped:\033[0m %s\n\n' "$*" >&2; exit 1; }

# --------------------------------------------------------------- 1. Python
say "1/5  Looking for Python 3.${MIN_MINOR} or newer"

PYTHON=""
for candidate in python3.14 python3.13 python3.12 python3.11 python3 python; do
    command -v "$candidate" >/dev/null 2>&1 || continue
    if "$candidate" -c "import sys; sys.exit(0 if sys.version_info >= (3, ${MIN_MINOR}) else 1)" 2>/dev/null; then
        PYTHON="$candidate"
        break
    fi
done

if [ -z "$PYTHON" ]; then
    die "no Python 3.${MIN_MINOR}+ found on this machine.
  Install it from https://www.python.org/downloads/ (or 'brew install python@3.12'
  on a Mac, 'sudo apt install python3.12-venv' on Debian/Ubuntu) and run this again."
fi
ok "$($PYTHON --version) at $(command -v "$PYTHON")"

# ---------------------------------------------------------- 2. Environment
say "2/5  Building an isolated environment in $VENV/"

if [ -d "$VENV" ]; then
    ok "reusing the existing $VENV/ (delete it and re-run for a clean slate)"
else
    "$PYTHON" -m venv "$VENV" 2>/dev/null || die "could not create a virtual environment.
  On Debian or Ubuntu this usually means the venv module is missing:
      sudo apt install python3-venv"
    ok "created $VENV/"
fi

VPY="$VENV/bin/python"
[ -x "$VPY" ] || VPY="$VENV/Scripts/python.exe"   # Git Bash on Windows
[ -x "$VPY" ] || die "the virtual environment looks broken. Delete $VENV/ and re-run."

# ------------------------------------------------------------- 3. Install
say "3/5  Installing trading.bot"

"$VPY" -m pip install --upgrade pip >/dev/null 2>&1 || warn "could not upgrade pip; continuing"

# The core package has no dependencies on purpose, so this half works offline.
# Only pytest needs the network, and the tool is fully usable without it.
if "$VPY" -m pip install -e ".[dev]" >/dev/null 2>&1; then
    ok "installed with the test suite"
    HAVE_TESTS=1
elif "$VPY" -m pip install -e . >/dev/null 2>&1; then
    warn "installed the app, but pytest could not be fetched (offline?).
      Everything works; only 'make test' is unavailable until you have a network."
    HAVE_TESTS=0
else
    die "install failed. Re-run with the output visible to see why:
      $VPY -m pip install -e \".[dev]\""
fi

# --------------------------------------------------------------- 4. Config
say "4/5  Preparing your local configuration"

mkdir -p reports
ok "reports/ ready for the trade journal"

if [ -f .env ]; then
    ok ".env already exists — keeping your current access token"
else
    TOKEN="$("$VPY" -c 'import secrets; print(secrets.token_urlsafe(24))')"
    cat > .env << ENVEOF
# Access token for the web app. Anyone with this token can read your signals
# and write to your journal, so treat it like a password.
#
# This file is gitignored and must stay that way. Delete it and re-run
# ./setup.sh to issue a new token.
TRADING_BOT_TOKEN=$TOKEN

# Optional: a Twelve Data key for live prices. Without it the app reads the
# CSV files in data/samples/, which are synthetic.
# TRADING_BOT_API_KEY=
ENVEOF
    chmod 600 .env 2>/dev/null || true
    ok "generated an access token in .env"
fi

# ---------------------------------------------------------------- 5. Verify
say "5/5  Checking that it actually works"

"$VPY" -c "import trading_bot; print('  ok  trading_bot', trading_bot.__version__, 'imports cleanly')"

if [ "$HAVE_TESTS" = "1" ]; then
    if "$VPY" -m pytest -q >/dev/null 2>&1; then
        ok "the full test suite passes"
    else
        die "the test suite failed on a fresh install. Something is wrong — see:
      $VPY -m pytest"
    fi
fi

"$VPY" -m trading_bot --config config/default.toml scan --no-journal >/dev/null 2>&1 \
    && ok "a live scan runs end to end" \
    || warn "the scan did not complete; the app is installed but check 'make scan'"

# ------------------------------------------------------------------- Done
TOKEN_VALUE="$(grep '^TRADING_BOT_TOKEN=' .env | cut -d= -f2-)"

cat << DONEEOF

$(printf '\033[1;32mSetup complete.\033[0m')

  Start it:        ./start.sh
  Then open:       http://localhost:8787/?token=$TOKEN_VALUE

  On your phone, on the same wifi, ./start.sh prints a second URL to use.
  Add it to your home screen and it installs as an app.

  Other things to try:
    ./.venv/bin/trading-bot scan        what to do right now
    make test                           run the 432 tests
    make demo                           scan, backtest and calibrate end to end

  Read SETUP.md for remote access, real market data, and security.

$(printf '\033[1;33mBefore you trade anything:\033[0m') the bundled data in data/samples/ is
  synthetic, and the strategy has no demonstrated edge on it. Load your own
  broker history and run 'backtest --split 0.7' before trusting a number.

DONEEOF
