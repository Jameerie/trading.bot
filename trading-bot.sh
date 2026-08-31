#!/usr/bin/env bash
#
#   trading.bot — one file that does the whole thing.
#
#   It clones the code if you do not have it, installs it into an isolated
#   environment, generates an access token, and opens the app in your browser.
#   Run it again any time to update and restart. There is nothing else to do.
#
#       bash trading-bot.sh
#
#   Options:
#       --port N       serve on a different port (default 8787)
#       --dir PATH     install somewhere other than ~/trading.bot
#       --scan         print what to do right now, then exit (no server)
#       --test         run the full test suite before starting
#       --no-open      do not open a browser
#       --local-only   bind to this machine only; no phone access
#       --update       pull the latest code even if nothing changed
#       --help         show this
#
#   This tool tells you what to do. It cannot place a trade, and it never will.

set -euo pipefail

REPO_URL="https://github.com/Jameerie/trading.bot"
INSTALL_DIR="${TRADING_BOT_DIR:-$HOME/trading.bot}"
PORT=8787
MIN_MINOR=11
HOST="0.0.0.0"
DO_OPEN=1
DO_TEST=0
DO_SCAN=0
DO_UPDATE=0
DIR_EXPLICIT=0

# ------------------------------------------------------------------ output
if [ -t 1 ]; then
    B=$'\033[1m'; DIM=$'\033[2m'; GRN=$'\033[32m'; YEL=$'\033[33m'
    RED=$'\033[31m'; OFF=$'\033[0m'
else
    B=""; DIM=""; GRN=""; YEL=""; RED=""; OFF=""
fi
step() { printf '\n%s%s%s\n' "$B" "$*" "$OFF"; }
ok()   { printf '  %sok%s  %s\n' "$GRN" "$OFF" "$*"; }
warn() { printf '  %s!!%s  %s\n' "$YEL" "$OFF" "$*"; }
die()  { printf '\n%sStopped:%s %s\n\n' "$RED" "$OFF" "$*" >&2; exit 1; }

# Print the header comment block, stopping at the first line of actual code.
# Falls back to a one-liner when piped from curl, where $0 is not readable.
usage() {
    if [ -r "$0" ]; then
        awk 'NR>1 && /^#/ { sub(/^# ?/, ""); print; next } NR>1 { exit }' "$0"
    else
        printf 'trading.bot — clones, installs and starts the app.\n'
        printf 'Options: --port N --dir PATH --scan --test --no-open --local-only --update\n'
    fi
    exit 0
}

# ------------------------------------------------------------------- flags
while [ $# -gt 0 ]; do
    case "$1" in
        --port)       PORT="${2:?--port needs a number}"; shift 2 ;;
        --dir)        INSTALL_DIR="${2:?--dir needs a path}"; DIR_EXPLICIT=1; shift 2 ;;
        --scan)       DO_SCAN=1; DO_OPEN=0; shift ;;
        --test)       DO_TEST=1; shift ;;
        --no-open)    DO_OPEN=0; shift ;;
        --local-only) HOST="127.0.0.1"; shift ;;
        --update)     DO_UPDATE=1; shift ;;
        -h|--help)    usage ;;
        *)            die "unknown option: $1  (try --help)" ;;
    esac
done

case "$PORT" in
    ''|*[!0-9]*) die "--port must be a number, got '$PORT'" ;;
esac

printf '\n%s  trading.bot%s  %sit tells you what to do; you place the trade%s\n' \
    "$B" "$OFF" "$DIM" "$OFF"

# --------------------------------------------------------------- 1. Python
step "1/5  Checking what this machine has"

PYTHON=""
for c in python3.14 python3.13 python3.12 python3.11 python3 python; do
    command -v "$c" >/dev/null 2>&1 || continue
    if "$c" -c "import sys; sys.exit(0 if sys.version_info >= (3, ${MIN_MINOR}) else 1)" 2>/dev/null; then
        PYTHON="$c"; break
    fi
done
[ -n "$PYTHON" ] || die "no Python 3.${MIN_MINOR} or newer found.
  macOS          brew install python@3.12
  Ubuntu/Debian  sudo apt install python3.12 python3.12-venv
  Windows        https://www.python.org/downloads/  (tick \"Add to PATH\")
  Then run this again."
ok "$("$PYTHON" --version)"

# ----------------------------------------------------------------- 2. Code
step "2/5  Getting the code"

# Running from inside a checkout already? Then use it where it is — unless the
# caller named a directory, in which case an explicit flag beats a guess.
if [ "$DIR_EXPLICIT" = "0" ] && [ -f "pyproject.toml" ] && [ -d "src/trading_bot" ]; then
    INSTALL_DIR="$PWD"
    ok "using the checkout you are already in: $INSTALL_DIR"
elif [ -d "$INSTALL_DIR/src/trading_bot" ]; then
    if [ "$DO_UPDATE" = "1" ] && command -v git >/dev/null 2>&1 && [ -d "$INSTALL_DIR/.git" ]; then
        git -C "$INSTALL_DIR" pull --ff-only >/dev/null 2>&1 \
            && ok "updated $INSTALL_DIR" \
            || warn "could not update (local changes?); using what is there"
    else
        ok "found an existing install at $INSTALL_DIR"
    fi
else
    command -v git >/dev/null 2>&1 || die "git is not installed, so the code cannot be fetched.
  macOS          xcode-select --install
  Ubuntu/Debian  sudo apt install git
  Or download the ZIP from $REPO_URL and run this script inside it."
    printf '  ..  cloning into %s\n' "$INSTALL_DIR"
    git clone --depth 1 "$REPO_URL" "$INSTALL_DIR" >/dev/null 2>&1 \
        || die "clone failed. Check your connection, or download the ZIP from
  $REPO_URL and run this script from inside the folder."
    ok "cloned to $INSTALL_DIR"
fi

cd "$INSTALL_DIR"

# ------------------------------------------------------------- 3. Install
step "3/5  Installing into an isolated environment"

VENV=".venv"
FIRST_RUN=0
if [ ! -d "$VENV" ]; then
    FIRST_RUN=1
    "$PYTHON" -m venv "$VENV" 2>/dev/null || die "could not create a virtual environment.
  On Debian or Ubuntu: sudo apt install python3-venv"
fi

VPY="$VENV/bin/python"
[ -x "$VPY" ] || VPY="$VENV/Scripts/python.exe"     # Git Bash on Windows
[ -x "$VPY" ] || die "the environment in $VENV looks broken. Delete it and re-run."

if [ "$FIRST_RUN" = "1" ] || [ "$DO_UPDATE" = "1" ]; then
    "$VPY" -m pip install --upgrade pip >/dev/null 2>&1 || true
    # The app itself has no dependencies, so this half works with no network.
    # Only pytest needs one, and losing it costs nothing but the test command.
    if "$VPY" -m pip install -e ".[dev]" >/dev/null 2>&1; then
        ok "installed, with the test suite"
    elif "$VPY" -m pip install -e . >/dev/null 2>&1; then
        warn "installed; pytest unavailable (offline?) so --test will not work"
    else
        die "install failed. Run this to see why:
      cd $INSTALL_DIR && $VPY -m pip install -e \".[dev]\""
    fi
else
    ok "already installed (pass --update to refresh)"
fi

# ---------------------------------------------------------------- 4. Setup
step "4/5  Preparing your configuration"

mkdir -p reports
if [ -f .env ]; then
    ok "keeping the access token already in .env"
else
    TOKEN="$("$VPY" -c 'import secrets; print(secrets.token_urlsafe(24))')"
    cat > .env << ENVEOF
# Access token for the web app. Anyone with this can read your signals and
# write to your journal, so treat it like a password. Delete this file and
# re-run to issue a new one.
TRADING_BOT_TOKEN=$TOKEN

# Optional Twelve Data key for live prices. Without it the app reads the
# synthetic CSVs in data/samples/.
# TRADING_BOT_API_KEY=
ENVEOF
    chmod 600 .env 2>/dev/null || true
    ok "generated a fresh access token"
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

# --------------------------------------------------------------- 5. Verify
step "5/5  Checking it actually works"

"$VPY" -c "import trading_bot" 2>/dev/null \
    && ok "the package imports cleanly" \
    || die "the package will not import. Something is wrong with the install."

if [ "$DO_TEST" = "1" ]; then
    printf '  ..  running the full test suite\n'
    # Run with the credentials cleared. The suite is hermetic about this now,
    # but an older checkout is not, and a token in .env would turn it red.
    env -u TRADING_BOT_TOKEN -u TRADING_BOT_API_KEY "$VPY" -m pytest -q >/dev/null 2>&1 \
        && ok "all tests pass" \
        || die "the test suite failed. See it with:
      cd $INSTALL_DIR && $VPY -m pytest"
fi

# ------------------------------------------------------------------- Scan
if [ "$DO_SCAN" = "1" ]; then
    printf '\n'
    exec "$VPY" -m trading_bot --config config/default.toml scan
fi

# ------------------------------------------------------------------ Serve
if "$VPY" - "$PORT" << 'PYEOF' 2>/dev/null
import socket, sys
s = socket.socket()
try:
    s.bind(("127.0.0.1", int(sys.argv[1])))
except OSError:
    sys.exit(1)
finally:
    s.close()
PYEOF
then :; else
    die "port $PORT is already in use — trading.bot may already be running.
  Open http://localhost:$PORT/ , or start this one elsewhere:
      bash $0 --port $((PORT + 1))"
fi

URL="http://localhost:$PORT/?token=${TRADING_BOT_TOKEN:-}"

if [ "$DO_OPEN" = "1" ]; then
    ( sleep 2
      if command -v open >/dev/null 2>&1;       then open "$URL"
      elif command -v xdg-open >/dev/null 2>&1; then xdg-open "$URL"
      fi ) >/dev/null 2>&1 &
fi

if [ "$FIRST_RUN" = "1" ]; then
    cat << NOTEEOF

  ${YEL}Before you trade on any of this:${OFF} the bundled data in data/samples/ is
  synthetic, and the strategy has no demonstrated edge on it. Load your own
  broker history and run a backtest with --split 0.7 before believing a number.
  SETUP.md explains how.
NOTEEOF
fi

printf '\n%sStarting. Ctrl-C to stop.%s\n' "$B" "$OFF"
exec "$VPY" -m trading_bot --config config/default.toml serve --host "$HOST" --port "$PORT"
