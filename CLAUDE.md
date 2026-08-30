# CLAUDE.md

Project instructions for Claude Code working in this repository.

---

## 1. What this project is

**trading.bot** is a **forex trading advisor**, not an execution bot.

The README states the goal precisely: *"The goal is to see what to do, not leave it to do the
trades."* That sentence is the product spec. The system reads market data, finds high-quality
setups, and prints a **signal card** telling the human exactly what to do — pair, direction,
entry, stop loss, take profit, position size, risk-to-reward, and the reasoning behind it.

**The human places the trade. The software never does.**

### Hard product rules

| Rule | Value | Enforced in |
|---|---|---|
| Minimum risk-to-reward | **1:4** | `risk.py` — signals below the floor are rejected, never rounded up |
| Trade execution | **Never** | No broker order API exists in this codebase. Do not add one. |
| Direction of bias | Long and short, symmetric | `strategy/` |
| Win-rate target | 85% (aspiration, measured not assumed) | `metrics.py`, `calibrate` command |

### On the 85% win rate — read this before "improving" the numbers

An 85% win rate at 1:4 R:R implies an expectancy of roughly +4.15R per trade. That is far
outside what published forex research or any credible track record shows. Treat 85% as a
**selectivity target to aim at**, not a fact to reproduce in a report.

Therefore this codebase is built so that the win rate is **measured honestly and reported with
its uncertainty**, never asserted:

- `metrics.py` reports a **Wilson score confidence interval** alongside every win rate, so a
  7-for-8 sample cannot masquerade as 87.5% skill.
- `QualityGate` marks a strategy `MEETS TARGET` only when the *lower bound* of the interval
  clears the target — not the point estimate.
- The `calibrate` command sweeps the confluence threshold and shows the real trade-off:
  higher selectivity buys a higher win rate and costs trade frequency.

**Never** do any of the following to make results look better. Each one is a way of lying:

- Do not tune parameters on the full dataset and then report those same in-sample results as
  performance. Use `--split` (walk-forward / out-of-sample) and report the out-of-sample number.
- Do not silently drop losing trades, cap the loss side, or filter trades after knowing outcomes.
- Do not let a bar's high/low be used for a decision made at that bar's open. Look-ahead bias is
  the single easiest way to manufacture an 85% win rate that does not exist. `backtest.py` has
  explicit guards; keep them.
- Do not report win rate without trade count and out-of-sample flag next to it.
- Do not remove the spread and commission model to improve results.

If a change makes the numbers better, first ask whether it made the *simulation* better or the
*strategy* better. Say which in the commit message.

---

## 2. Repository map

```
src/trading_bot/
  models.py        Candle, Signal, Trade, Direction, Outcome — frozen dataclasses
  config.py        TOML config loading + validation (stdlib tomllib)
  instruments.py   Pip size, contract size, quote conventions per pair
  indicators.py    EMA, SMA, ATR, RSI, ADX, rolling helpers — pure functions on lists
  structure.py     Swing points, BOS/CHoCH, order blocks, fair value gaps, liquidity sweeps
  sessions.py      London / New York / Tokyo session windows, UTC-only
  strategy/
    base.py        Strategy protocol — evaluate(history) -> Signal | None
    confluence.py  Weighted confluence scoring; the quality dial
    trend_pullback.py  Default strategy: HTF trend + structure break + pullback entry
  risk.py          R:R floor enforcement, stop placement, position sizing
  signals.py       Signal construction and the human-readable "what to do" card
  backtest.py      Bar-by-bar simulation with intrabar SL/TP resolution
  metrics.py       Win rate + Wilson CI, expectancy, profit factor, drawdown, streaks
  calibrate.py     Threshold sweep to find the selectivity that meets the target
  journal.py       Append-only JSONL record of every signal issued
  report.py        Markdown / terminal rendering
  cli.py           Entry point: scan, backtest, calibrate, journal, data
  data/
    base.py        DataSource protocol
    csv_source.py  OHLCV CSV loader
    synthetic.py   Deterministic generator — used by tests and demos
    rest_source.py Twelve Data / generic REST via stdlib urllib
```

---

## 3. Conventions

**Dependencies.** The core package uses the **Python standard library only** (3.11+). No pandas,
no numpy, no requests. This is deliberate: the tool must run on any machine with Python and no
install step. If you believe a dependency is unavoidable, put it behind an optional extra in
`pyproject.toml` and keep the core import-clean. `python -c "import trading_bot"` must work in a
bare interpreter.

**Prices and money.** Prices are `float`. Never compare prices with `==`; use `math.isclose` or
the pip-tolerance helpers in `instruments.py`. All pip arithmetic goes through
`instruments.pips_between` / `price_from_pips` — never hardcode `0.0001`, because JPY pairs are
`0.01`.

**Time.** Every timestamp is timezone-aware UTC. Naive datetimes are rejected at the data-source
boundary, not deeper in. Session logic converts to exchange time only inside `sessions.py`.

**Data shape.** A "history" is a list of `Candle` ordered oldest → newest. Index `-1` is the most
recent *closed* candle. Functions that evaluate a setup receive only candles up to and including
the decision bar; passing future bars is a bug.

**Purity.** `indicators.py` and `structure.py` are pure functions with no I/O and no globals. They
are the most-tested part of the codebase; keep them that way.

**Errors.** Raise `ConfigError`, `DataError`, or `RiskError` from `errors.py`. Do not raise bare
`Exception`, and do not `except: pass` around price math.

**Style.** 4-space indent, type hints on all public functions, docstrings that say *why* rather
than restating the signature. Match the surrounding file.

---

## 4. Commands

```bash
make test            # pytest suite
make lint            # compile-check + import-clean check
make demo            # end-to-end run on bundled sample data

python -m trading_bot scan     --config config/default.toml     # what to do right now
python -m trading_bot backtest --csv data/samples/EURUSD_H1.csv --split 0.7
python -m trading_bot calibrate --csv data/samples/EURUSD_H1.csv
python -m trading_bot journal  --list
```

`scan` is the primary user-facing command. Its output is the product.

---

## 5. Working agreements

- **Tests are not optional.** Any change to `indicators.py`, `structure.py`, `risk.py`, or
  `backtest.py` ships with a test. The R:R floor and the look-ahead guards have dedicated tests;
  if you change behaviour there, the test must be updated deliberately and the commit must say why.
- **Run `make test` before every commit.** A red suite is never pushed.
- **No secrets in the repo.** API keys come from environment variables (`TRADING_BOT_API_KEY`).
  `config/*.toml` holds no credentials.
- **No network in tests.** `rest_source.py` is never exercised by the suite; use `synthetic.py`.
- **Determinism.** Same input data plus same config produces byte-identical output. Seed anything
  random. A non-deterministic backtest is a broken backtest.
- **Branch.** Development happens on `claude/system-creation-claude-d-td27f5`.

---

## 6. What not to build

- Order placement, broker authentication, or anything that can move real money.
- Auto-compounding or martingale position sizing. Risk per trade is fixed by config.
- Claims of guaranteed returns in docs, output, or the README.
- A machine-learning model trained on the same data used to report performance.

The tool's job is to tell a human what it sees, how confident it is, and what it would risk. It
stops there — on purpose.
