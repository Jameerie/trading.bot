# trading.bot

A forex **trading advisor**. It reads **64 instruments** — every major, every major
cross, thirty exotics and the metals — finds setups that pay at least **1:4**, and
tells you what to do: pair, direction, entry, stop, target, size, how to place the
order, when to watch it, what would make it wrong, and what to do afterwards. You
place the trade. The software never does.

> The goal is to see what to do, not leave it to do the trades.

Every signal is a **dated, falsifiable prediction** — written down before the outcome
exists, carrying a deadline and the measured frequency of claims like it coming true.
Those predictions are then settled against real candles, and the resulting forward
record is kept strictly apart from any backtest. A backtest replays outcomes that were
already in the file; it cannot put a single entry on that board.

Times are shown in your own timezone (`Africa/Lagos` by default, UTC+1, no daylight
saving), with the London, New York and Tokyo windows converted to your wall clock, so
"the London open" means 08:00 on your morning rather than 07:00 on someone else's.

## What it looks like

```
==============================================================================
  SELL GBPAUD   [A]  confidence 79%   16.8R
  British pound against Australian dollar
  H1 - Mon 31 Aug 14:00 WAT (13:00 UTC) - London/NY overlap
==============================================================================

  IN ONE LINE
    Sell the British pound against the Australian dollar at around 1.15194,
    because it looks likely to fall to 1.10480.
    You are risking 99.60 USD to make 1,672.18 USD.
    If it reaches 1.15443 instead, you lose the 99.60 and the trade is over.

  WHAT TO DO
    Entry        1.15194   (at or near this price)
    Stop loss    1.15443   (24.9 pips risk)
    Take profit  1.10480   (471.4 pips reward)
    Size         0.40 lots (40,000 units)
    Risking      99.60 USD  to make  1,672.18 USD

  THE PREDICTION
    Claim        GBPAUD falls to 1.10480 before rising to 1.15443, entered at
                 the next bar's open
    Enter by     Mon 31 Aug 17:00 WAT (16:00 UTC) (3 bars) - then it is stale
    Resolves by  Thu 10 Sep 22:00 WAT (21:00 UTC) (200 bars, in 10 days)
    Base rate    31% of 26 comparable GBPAUD setups reached target before stop
                 (16%-51% at 95% confidence)

  HOW TO PLACE IT  (type these into your broker, in this order)
    1. Symbol       GBPAUD  (British pound against Australian dollar)
    2. Order type   Sell Limit   - not a market order
    3. Price        1.15194
    4. Volume       0.40 lots   (40,000 units)
    5. Stop loss    1.15443   (24.9 pips away)
    6. Take profit  1.10480   (471.4 pips away)

    Before you hit confirm, check the ticket says 0.40 lots and not 40.

  WHEN TO WATCH IT
    GBPAUD is liquid in Sydney 22:00-07:00; Tokyo 01:00-10:00;
    London 08:00-17:00 WAT
    Next Sydney open: Mon 31 Aug 22:00 WAT (21:00 UTC) (in 8h)

------------------------------------------------------------------------------
  WHY  (96 of 122 confluence points)
    + H4 structure is trending down (+20)
    + CHOCH short at 1.15259, 11 bar(s) ago (+15)
    + market structure shows lower lows and lower highs (+15)
    + EMA 21 < EMA 50 < EMA 200 (+12)
    ...

  WHAT WOULD MAKE THIS WRONG / WHILE IT IS RUNNING / WHAT IF... / AFTERWARDS
    ...four more sections telling you exactly what to do at each stage.

------------------------------------------------------------------------------
  You place this trade. This tool does not and will not.
==============================================================================
```

And when nothing qualifies — which is most of the time — it says why, rather than
just no:

```
  USDJPY  -  no trade on H1
    Best case is a sell, scoring 75 of 122 points (61%).
    It needs 70% to become a signal, so it is 9 points of confluence short.

    Already true:
      + H4 structure is trending down
      + price is inside a short order block (157.58613-157.74877)
      + liquidity swept at 157.73734 and rejected

    Still needed - this is what to watch for:
      - swing sequence on this timeframe agrees (+15): the chart needs a clean
        run of lower lows and lower highs. A choppy sequence means no trend
      - moving averages stacked in order (+12): the 21 EMA needs to sit below
        the 50, and the 50 below the 200

    This is close. Put USDJPY on your watchlist and re-scan at the next bar close.
    Next chance for this pair to move: New York open, Mon 31 Aug 13:00 WAT.
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
bash trading-bot.sh --test          # run the 609 tests before starting
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
make test    # 609 tests, offline, ~40 seconds
```

### Commands

| Command | What it does |
|---|---|
| `serve` | Runs the web UI. Reachable from any device. |
| `scan` | Looks at the latest closed bar on every instrument and says what to do. **This is the product.** |
| `pairs` | **Win rate pair by pair**, corrected for the fact that you looked at sixty. |
| `forecast` | Live predictions, and the forward record of the ones already settled. |
| `backtest` | Measures the strategy on history. `--split 0.7` reports out-of-sample separately. |
| `calibrate` | Sweeps the selectivity threshold and shows the win-rate / trade-count trade-off. |
| `risk` | How much to risk per trade, and what it costs to be wrong. |
| `journal` | Lists issued signals, and records how they actually finished. |
| `data` | Generates sample CSVs or inspects one. |

```bash
python -m trading_bot serve --host 0.0.0.0            # reach it from your phone
python -m trading_bot scan                            # every instrument in your config
python -m trading_bot scan --symbols majors metals    # or a named group
python -m trading_bot scan --brief                    # prices and reasoning, no coaching
python -m trading_bot pairs --split 0.7               # which pairs are worth trading
python -m trading_bot forecast --resolve              # settle predictions against real candles
python -m trading_bot backtest --csv data/samples/EURUSD_H1.csv --split 0.7 --trades
python -m trading_bot risk --from-backtest --csv data/samples/EURUSD_H1.csv
python -m trading_bot journal --close "EURUSD@2024-05-01T14:00:00+00:00" --exit 1.0865
```

### Instruments

`data.symbols` takes individual pairs, group names, or a mix of both:

| Group | Count | What it is |
|---|--:|---|
| `majors` | 7 | The USD pairs. Tightest spreads, deepest books. |
| `crosses` | 21 | Every cross between the eight major currencies. |
| `exotics` | 30 | Scandinavian and emerging-market pairs. |
| `metals` | 6 | Gold, silver, platinum, palladium. |
| `core` | 29 | Majors + crosses + gold — the liquid set. |
| `all` | 64 | Everything. **The shipped default.** |

Expect most exotics to be rejected by the R:R floor rather than by the strategy: a
40-pip spread eats a 1:4 trade before it starts, and the floor saying so is the floor
working.

## The app

Seven views, all working from the same engine the CLI uses:

- **Scan** — signal cards with a price chart, entry/stop/target, lot size, the full
  reasoning, the prediction it amounts to, and a collapsible playbook for placing it.
  A "no setup" card shows which checks passed, which failed, and what would have to
  change. When several signals are live at once, a netted currency-exposure card sits
  above them.
- **Pairs** — win rate per instrument, with both the raw interval and the one
  corrected for how many pairs were inspected, plus a per-currency breakdown.
- **Predictions** — live claims with their deadlines in your own clock, and the
  forward record of the settled ones.
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
  and costs trade frequency. It reports where that actually lands on your data — and
  `calibrate --ceiling` sweeps the other dial, how far the target is allowed to sit,
  which on some data matters more than selectivity does.

### What the dials actually measured

The confluence threshold ships at **0.70**, and that is a measured choice rather than
an untouched default. Swept out-of-sample from 0.60 to 0.85 on the bundled samples,
turning it up costs trades without buying expectancy: 0.70 returned +0.57R per trade,
0.75 the same on fewer trades, and 0.85 fell to +0.25R on a third of the sample.
Raising it feels like more discipline and measures as less edge.

The reward ceiling is the dial worth your attention. On those same samples, lowering
`max_risk_reward` from 20 to 10 more than doubled out-of-sample expectancy — targets
were being planned at 12:1 and paying 2.5:1, the difference expiring on the time limit
rather than at a barrier. It ships at 20 anyway, because across a much wider set of
series the ordering reversed, and changing a default on the weaker of two conflicting
measurements is the curve-fitting this project exists to refuse. Sweep it yourself.

Every sample behind those numbers was under 30 trades. None of it is proof, and the
reports say so on every line.

A 1:4 system does not need a high win rate to make money — it is profitable above
about 20% — and the reports show expectancy alongside the win rate for that reason.

## Win rate by pair — and the trap in reading one

Scanning sixty instruments instead of three is straightforwardly good: a setup you
never looked at is a setup you never had. *Judging* sixty is where it gets dangerous,
and `pairs` is built for the dangerous half.

```bash
python -m trading_bot pairs --split 0.7
```

Three things happen there that a plain per-pair table gets wrong:

- **Every pair asked about appears**, including the ones with no data and the ones
  that produced no setups. A table listing only the pairs that produced trades has
  already been filtered on its own outcome.
- **The intervals widen for the fact that you looked at sixty pairs.** At 95%
  confidence, three pairs in sixty clear the bar on noise alone — and the eye goes
  straight to them. Šidák's correction raises the per-pair standard so that *all*
  the intervals hold together at 95%: across 60 pairs that is 99.91% each. A pair is
  marked `TRADE IT` only if its **corrected** lower bound beats chance and its
  expectancy stays positive at the low bound.
- **Currencies are scored as well as pairs.** EURUSD, EURGBP and EURJPY are not three
  independent readings — they share a leg. The per-currency table shows whether it is
  the euro that has been paying or one pair that got lucky.

Ranking is by the lower bound of expectancy, never by win rate. Win rate without its
ratio puts a 45%-at-1.5:1 pair above a 30%-at-6:1 pair, and the second one makes twice
the money.

### And whether any of that is worth acting on

"Trade the pairs that measured well" is a hypothesis, not a fact, so the tool tests
its own recommendation:

```bash
python -m trading_bot pairs --persistence
```

Each pair's history is split in half; pairs are chosen on the first half alone, then
both the full universe and the chosen subset are measured on the second. Nothing from
the second half is visible when the choice is made. If the two columns come out the
same, the finding is that **this strategy's edge is not pair-specific** — filtering
the universe would cost you trades and buy nothing, and the effort belongs on
correlation instead. That is a result, not a failed run, and the report says so.

### Four euro longs is one bet

Scanning sixty instruments produces correlated signals, and a list of six cards looks
like diversification when it is not. Whenever a scan turns up more than one setup, the
currency legs are netted and reported: long EURUSD, long EURJPY and short EURGBP is a
single euro position at three times the intended size, and it will win or lose as one.

The block warns, ranks the setups by expected value — reading the *lower bound* of a
measured win rate, never the point estimate — and suggests a subset that fits inside
`account.max_concurrent_risk_pct`, with a reason printed beside every signal it left
out. It cannot drop a signal. Nothing here can; that would be the software deciding.

## Predictions, not replays

A backtest replays trades whose outcome is already in the file. It is useful for
estimating how often a setup like this one has worked, and worthless as evidence that
the tool can call the next one. So the two are kept apart:

- A **prediction** is a claim made at a bar close about bars that do not exist yet:
  *price reaches the take profit before the stop loss, entering at the next bar's open,
  within N bars.* It carries a deadline in your own clock and is journalled when issued.
- A **base rate** is the measured frequency of that claim coming true on this pair's
  history, with its sample size and interval attached. When there is no sample worth
  quoting, the card says the prediction is **unscored** rather than inventing a number.
- The **forward record** counts only predictions resolved after they were made. It
  starts at zero the day you install this, which is exactly right: nobody has a track
  record they have not yet earned.

```bash
python -m trading_bot forecast --resolve   # settle open predictions against real candles
python -m trading_bot forecast             # live claims and the forward record
```

Settlement reuses the backtester's own resolver — stop wins a tied bar, gaps fill at
the open, costs charged — so a live prediction is scored by the identical rule that
produced its base rate. A prediction that has not resolved stays **open**; running out
of data is not the same as running out of time, and force-closing one would quietly
convert an unfinished claim into a scored one.

## Being told what to do, step by step

The card does not stop at four prices. For every signal it prints the trade in plain
English, then the order ticket field by field in the words a broker platform uses, the
hours that pair is actually liquid in *your* timezone, what would invalidate the idea
before it fills, what to do while it runs, what to do about a gap or a missed entry,
and the command that records the outcome afterwards.

The management advice is "do nothing", and it explains why: every number on the card
came from a simulation that placed the stop and the target and did not touch them.
Trail the stop and you may well do better — but you are then trading a system nobody
has measured, and the win rate on the card has stopped describing it.

When nothing qualifies — which is most of the time — the output says which conditions
were already met, which are missing, what each missing one would need in order to fire,
and whether the pair is worth watching or worth leaving alone.

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

`[display]` sets the timezone every time is shown in, and how much guidance each card
carries:

```toml
[display]
timezone = "Africa/Lagos"   # UTC+1, no daylight saving; session windows shift with it
detail   = "full"           # "brief" prints prices and reasoning only
```

The timezone is a display concern only. Candles, sessions and the journal are UTC
everywhere and stay that way — the moment two components disagree about what time a
bar opened, every backtest becomes fiction.

Everything else lives in `config/default.toml`. Unknown keys are rejected rather than
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

Only EURUSD, GBPUSD and USDJPY are bundled, and the shipped config scans all 64
instruments, so a fresh clone has no data for 61 of them. That is reported as one
line naming the command that fixes it, not as 61 errors, and the pairs stay listed:
a pair with no data is **unmeasured, not clear**. Fill them in one pass:

```bash
python -m trading_bot data --fetch    --only-missing   # real candles; needs a provider key
python -m trading_bot data --generate --only-missing   # synthetic; pipeline testing only
python -m trading_bot data --inspect data/samples/EURUSD_H1.csv
```

`--fetch` writes one CSV per symbol from the configured provider and pauses between
requests for the free tier's rate limit (`--pause 0` if your plan allows more).
`--only-missing` leaves files you already have alone, so a broker export you dropped
in yourself is never overwritten by a generated one.

## Project layout

```
src/trading_bot/
  models.py       indicators.py    structure.py     resample.py
  risk.py         signals.py       scanner.py       precompute.py
  backtest.py     metrics.py       calibrate.py     report.py
  config.py       instruments.py   sessions.py      journal.py
  risk_analysis.py                 limits.py
  strategy/       data/            web/             cli.py
tests/            609 tests, no network, deterministic
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

- Single-position and single-timeframe per instrument. Correlation across concurrent
  signals is measured and reported, but nothing sizes a portfolio for you.
- The web app is single-user and holds no accounts. Set `TRADING_BOT_TOKEN`
  before exposing it beyond localhost.
- Pip value for crosses that share no currency with the account is approximated, and
  signals say so.
- Exotic spreads are catalogued averages, not your broker's. Check them before trading
  a pair the R:R floor only just lets through.
- Stop bounds for metals and exotics are normalised by a per-instrument scale factor —
  a coarse conversion so an 8-60 pip window written for majors means something on gold.
  It widens the window a structural stop may sit in; it never moves the stop.
- The tool reads no economic calendar. It cannot know that CPI is out in ten minutes.
- The REST source covers Twelve Data only; adding a provider means one class.
- **It has no live track record.** Nothing here has been validated on real forward
  data — which is the reason `forecast` exists, and the reason that board starts at
  zero rather than being seeded from a backtest.

## Licence

MIT.

---

**This is not financial advice.** Trading forex on leverage can lose more than your
deposit. The tool reports what it sees and what it would risk; every decision, and
every consequence, is yours.
