# trading.bot

A forex **trading advisor**. It reads the market, finds setups that pay at least
**1:4**, and tells you what to do — pair, direction, entry, stop, target, size, and
why. You place the trade. The software never does.

> The goal is to see what to do, not leave it to do the trades.

## What it looks like

```
==========================================================================
  SELL EURUSD   [B]  confidence 72%   4.0R
  H1 - 2024-01-09 15:00 UTC - London/NY overlap
==========================================================================

  WHAT TO DO
    Entry        1.10409   (at or near this price)
    Stop loss    1.10588   (17.9 pips risk)
    Take profit  1.09643   (76.6 pips reward)
    Size         0.55 lots (55,000 units)
    Risking      98.45 USD  to make  393.80 USD

--------------------------------------------------------------------------
  WHY  (88 of 122 confluence points)
    + BOS short at 1.10311, 3 bar(s) ago (+15)
    + market structure shows lower lows and lower highs (+15)
    + EMA 21 < EMA 50 < EMA 200 (+12)
    + price is inside a short order block (1.10359-1.10484) (+12)
    + ADX 36.1 is above the 20 trend threshold (+8)
    + EMA 21 slope is falling (+8)
    + +DI 17.4 vs -DI 25.3 favours short (+6)
    + RSI 39.3 leaves room to run (+6)
    + inside the london/newyork session window (+6)

--------------------------------------------------------------------------
  You place this trade. This tool does not and will not.
==========================================================================
```

## Quick start

One file does everything — it fetches the code, installs it, and opens the app:

```bash
curl -fsSL https://raw.githubusercontent.com/Jameerie/trading.bot/main/trading-bot.sh -o trading-bot.sh
bash trading-bot.sh
```

Python 3.11+ and git are the only requirements, and it tells you how to get
either if it is missing. Run the same command again any time to restart; add
`--update` to pull the latest code first.

```bash
bash trading-bot.sh --scan          # just tell me what to do right now
bash trading-bot.sh --port 9000     # serve somewhere else
bash trading-bot.sh --local-only    # this machine only, no phone access
bash trading-bot.sh --test          # run the 433 tests before starting
bash trading-bot.sh --help          # everything else
```

It serves on your local network too, so the second URL it prints opens on your
phone and installs to the home screen as an app. For access from outside your
network, see **[SETUP.md](SETUP.md)**.

### On Windows

Download **[trading-bot.bat](trading-bot.bat)** and double-click it, or from a
terminal:

```
trading-bot.bat
```

Same flags as above (`--scan`, `--port`, `--test`, `--help`). It needs Python
3.11+ from [python.org](https://www.python.org/downloads/) — tick *"Add
python.exe to PATH"* on the first install screen — and
[git](https://git-scm.com/download/win). It tells you if either is missing.

### Already cloned?

`./setup.sh` once, then `./start.sh` whenever you want it. Same result, and
`make setup` / `make start` wrap both.

### Neither?

The core has no dependencies, so a checkout runs directly:

```bash
export PYTHONPATH=src
python -m trading_bot --config config/default.toml scan
```

```bash
make demo    # scan, backtest and calibrate on the bundled data
make test    # 433 tests, offline, ~25 seconds
```

### Commands

| Command | What it does |
|---|---|
| `serve` | Runs the web UI. Reachable from any device. |
| `scan` | Looks at the latest closed bar and says what to do. **This is the product.** |
| `backtest` | Measures the strategy on history. `--split 0.7` reports out-of-sample separately. |
| `calibrate` | Sweeps the selectivity threshold and shows the win-rate / trade-count trade-off. |
| `risk` | How much to risk per trade, and what it costs to be wrong. |
| `journal` | Lists issued signals, and records how they actually finished. |
| `data` | Generates sample CSVs or inspects one. |

```bash
python -m trading_bot serve --host 0.0.0.0            # reach it from your phone
python -m trading_bot scan --symbols EURUSD GBPUSD --compact
python -m trading_bot backtest --csv data/samples/EURUSD_H1.csv --split 0.7 --trades
python -m trading_bot risk --from-backtest --csv data/samples/EURUSD_H1.csv
python -m trading_bot journal --close "EURUSD@2024-05-01T14:00:00+00:00" --exit 1.0865
```

## The app

Five views, all working from the same engine the CLI uses:

- **Scan** — signal cards with a price chart, entry/stop/target, lot size and the
  full reasoning. Or a "no setup" card showing how close it came.
- **Backtest** — metrics, equity curve, and the in-sample vs out-of-sample gap.
- **Calibrate** — the selectivity sweep as a table.
- **Sizing** — Kelly, the recommended risk, and the drawdown each level costs.
- **Journal** — what you were advised, and what actually happened.

It is an installable PWA: the shell works offline, but scans deliberately do not.
A cached signal priced against a market that has since moved is a wrong answer,
not a degraded one.

## About the 85% win rate

The stated goal is an 85%+ win rate at 1:4 risk-to-reward. That combination implies
roughly **+4.15R of expectancy per trade**, which is far beyond anything published
research or a credible verified track record supports. It is treated here as a
**target to aim at and measure against**, never as a result to reproduce.

So the system is built to be honest about it rather than flattering:

- **Every win rate is reported with a Wilson confidence interval.** Seven wins from
  eight trades is 87.5%, and it is also consistent with a coin flip. The interval says so.
- **The quality gate tests the interval's lower bound**, not the point estimate. A
  strategy is only marked `MEETS TARGET` when the evidence rules out its being worse.
- **Fewer than 30 trades produces no claim at all** — the report says `INSUFFICIENT DATA`
  instead of quoting a number.
- **`calibrate` shows the real trade-off.** Higher selectivity buys a higher win rate
  and costs trade frequency. It reports where that actually lands on your data.

A 1:4 system does not need a high win rate to make money — it is profitable above
about 20% — and the reports show expectancy alongside the win rate for that reason.

## Closing the loop

A signal generator that never learns from its own advice is a slot machine with
opinions. Every signal is journalled when issued, and you record how it finished:

```bash
python -m trading_bot journal --close "EURUSD@2024-05-01T14:00:00+00:00" --exit 1.0865
```

The journal is append-only JSONL — a close is a *new line* referencing the
original signal, never an edit of it — so the history is tamper-evident. The
**Live performance** panel then measures your real trades in the same units as
the backtest, with the same confidence-interval treatment, so you can see whether
the simulation is telling the truth.

## How the strategy works

The default strategy trades **trend continuation on a pullback**. It looks for a
market with an established higher-timeframe bias, waits for structure to break in
that direction, then enters as price retraces into the zone the impulse left behind.

Entering on the pullback rather than the breakout is what lets a tight stop and a
distant target coexist. Chasing a breakout gives a wide stop and a near target — the
exact inverse of a 1:4 trade.

A setup must score enough **confluence** to qualify. Twelve independent checks each
carry a weight, and the score is the fraction that fired:

| Check | Weight | Check | Weight |
|---|---|---|---|
| Higher-timeframe trend agrees | 20 | Liquidity sweep rejected | 8 |
| Break of structure / CHoCH | 15 | Directional index agrees | 6 |
| Swing structure agrees | 15 | RSI has room to run | 6 |
| EMA stack ordered | 12 | Inside a liquid session | 6 |
| Price in a pullback zone | 12 | Decisive entry bar | 6 |
| ADX above threshold | 8 | EMA slope agrees | 8 |

Setups scoring below `strategy.min_confluence` are discarded, and so is anything that
cannot pay 1:4 after costs. Both rejections are silent and normal — a selective
system says "no setup" far more often than it says "here is one".

## Why the backtest numbers are trustworthy

Every modelling choice is taken pessimistically, and each one has a test pinning it:

- **Signals fire on a closed bar; fills happen at the next bar's open.** You cannot
  trade a bar you are still inside.
- **A bar touching both stop and target counts as a loss.** OHLC cannot say which came
  first. For a 1:4 system this assumption alone can swing the win rate by tens of
  points, so it is always resolved against us.
- **Gaps fill at the open**, not at the level — so a loss can exceed 1R.
- **Spread, slippage and commission are charged** on entry and exit.
- **One position per symbol at a time**, so a single idea is not counted as twenty
  overlapping wins.
- **Swing points carry a confirmation delay.** A pivot is invisible until the bars
  that define it have closed. This is the load-bearing guarantee of the whole project,
  and `tests/test_structure.py` asserts it from several directions.

## Configuration

Everything lives in `config/default.toml`. Unknown keys are rejected rather than
ignored, so a typo surfaces immediately instead of leaving you trading a setting you
thought you had changed.

The one value you cannot lower is `risk.min_risk_reward` — the loader refuses anything
below 4.0. That floor is the product, not a default.

API keys are read from the environment (`TRADING_BOT_API_KEY` by default) and never
from the config file.

## Data

Three sources: `csv` (default), `synthetic`, and `rest` (Twelve Data). The bundled
files in `data/samples/` are **synthetic** — generated by a seeded random walk so the
demo and tests run offline and deterministically. They are not a market, and results
from them say nothing about live performance. Point the tool at your own broker's
exported history to get numbers that mean something.

```bash
python -m trading_bot data --generate --symbols EURUSD --bars 3000
python -m trading_bot data --inspect data/samples/EURUSD_H1.csv
```

## Project layout

```
src/trading_bot/
  models.py       indicators.py    structure.py     resample.py
  risk.py         signals.py       scanner.py       precompute.py
  backtest.py     metrics.py       calibrate.py     report.py
  config.py       instruments.py   sessions.py      journal.py
  risk_analysis.py                 limits.py
  strategy/       data/            web/             cli.py
tests/            433 tests, no network, deterministic
config/           default.toml
data/samples/     synthetic OHLCV
deploy/           systemd unit
setup.sh          one-time setup; start.sh runs the app
```

| Document | What it covers |
|---|---|
| [SETUP.md](SETUP.md) | Install, phone access, Docker, VPS, real data, troubleshooting |
| [TESTING.md](TESTING.md) | How testing is conducted, layer by layer |
| [CLAUDE.md](CLAUDE.md) | Architecture and the rules that keep results honest |

## Limitations

- Single-position, single-timeframe, one instrument at a time. No portfolio-level
  correlation handling.
- The web app is single-user and holds no accounts. Set `TRADING_BOT_TOKEN`
  before exposing it beyond localhost.
- Pip value for crosses that share no currency with the account is approximated, and
  signals say so.
- The REST source covers Twelve Data only; adding a provider means one class.
- **It has no live track record.** Nothing here has been validated on real forward data.

## Licence

MIT.

---

**This is not financial advice.** Trading forex on leverage can lose more than your
deposit. The tool reports what it sees and what it would risk; every decision, and
every consequence, is yours.
