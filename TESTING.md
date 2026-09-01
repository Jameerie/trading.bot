# How testing is conducted

700 tests, no network, fully deterministic, ~60 seconds.

```bash
make test                              # everything
python3 -m pytest tests/ -v            # verbose
python3 -m pytest tests/test_risk.py   # one file
python3 -m pytest -k "look_ahead"      # one concern across files
```

`pyproject.toml` sets `pythonpath = ["src", "tests"]`, so plain `pytest` works
with no environment setup.

---

## The principle behind the suite

This project's output is a **claim about accuracy**. A backtest that reports 60%
when the truth is 25% is worse than no tool at all, because it will be believed
and traded. So the suite is not organised around code coverage — it is organised
around *the specific ways a trading system lies to its owner*, with a test
pinning each one shut.

There are four such ways, and they map directly onto the test files:

| Failure | What it does | Where it is pinned |
|---|---|---|
| **Look-ahead** | Uses data that did not exist yet, inflating everything | `test_structure.py`, `test_precompute.py` |
| **Ratio flattery** | Scores a result against a target the trades never reached | `test_edge.py` |
| **Optimistic fills** | Assumes the good outcome when the bar is ambiguous | `test_backtest.py` |
| **Small-sample bravado** | Reports 7-from-8 as an 87% win rate | `test_metrics.py` |
| **Sizing on a hoped edge** | Bets Kelly on a rate the sample cannot support | `test_risk_analysis.py` |
| **Selection** | Ranks sixty pairs and sells the luckiest as an edge | `test_pairs.py` |
| **Replay as record** | Presents backtested history as a track record | `test_forecast.py` |
| **Correlation blindness** | Counts four euro longs as four independent bets | `test_exposure.py` |
| **Diary sold as record** | Lets a replayed outcome onto the forward ledger, or scores a call against a plan it did not fill | `test_ledger.py` |

## Full inventory

| File | Tests | Covers |
|---|--:|---|
| `test_web.py` | 65 | API handlers, auth, path traversal, static serving, body limits, the ledger endpoints |
| `test_models_config.py` | 72 | Candle validation, instruments, config loading, sessions |
| `test_data.py` | 54 | CSV parsing, vendor date formats, resampling, synthetic generator, filling a directory |
| `test_signals_journal.py` | 37 | Signal construction, card rendering, confluence engine, scanning |
| `test_risk_analysis.py` | 30 | Expectancy, Kelly, Monte Carlo sizing, misestimation |
| `test_cli.py` | 53 | Every command end to end through the real entry point, `ledger` included |
| `test_risk.py` | 28 | **The 1:4 floor**, stop placement, position sizing, pip value |
| `test_structure.py` | 27 | Swings, trend, BOS/CHoCH, gaps, sweeps, **look-ahead guards** |
| `test_journal_outcomes.py` | 31 | Realised R, closing trades, append-only history, live metrics, snapshots and close detail |
| `test_metrics.py` | 35 | Wilson intervals, drawdown, streaks, **the quality gate** |
| `test_indicators.py` | 20 | EMA, SMA, RSI, ATR, ADX values and index alignment |
| `test_backtest.py` | 20 | **Simulation realism** — fills, tie-breaking, gaps, overlap; the fill on the record |
| `test_precompute.py` |  7 | Cached and uncached evaluation produce identical signals |
| `test_edge.py` | 20 | Edge over chance; a planned ratio cannot manufacture one |
| `test_limits.py` | 22 | Daily-loss and drawdown breakers; advisory, never blocking |
| `test_playbook.py` | 26 | The guidance printed with every signal; the near-miss explanation |
| `test_forecast.py` | 24 | Predictions, deadlines, settlement, **the forward record**, what a close carries |
| `test_clock.py` | 21 | Local-time rendering; session windows in the reader's own clock |
| `test_pairs.py` | 26 | Win rate by pair, **the multiple-comparison correction** |
| `test_exposure.py` | 15 | Netted currency exposure; the module warns and never acts |
| `test_ledger.py` | 41 | Case files; **a replay equals the backtest and never touches the journal**; settlement detail; scorecards; what to do about an open call |
| `test_dukascopy.py` | 26 | The datafeed decoder on files built in the test; the scale check; the month walk |
| **Total** | **700** | |

### The three failures the wider universe introduced

Scanning 64 instruments rather than 3 creates failure modes the smaller tool could
not have had, and each has a test pinning it:

- **Selection.** Rank sixty win rates and the top row is by construction the luckiest
  row. `test_pairs.py` asserts that intervals widen with the number of pairs inspected,
  that a pair below the minimum sample never earns a verdict however good it looks, and
  that a losing pair can never be ranked above a profitable one.
- **Replay sold as record.** `test_forecast.py` asserts directly that a backtest
  producing trades adds *nothing* to the forward scoreboard, and that a prediction with
  no resolution yet stays open rather than being force-closed at whatever price
  happened to be showing.
- **Correlation.** `test_exposure.py` asserts the netting arithmetic, and that the
  module never removes a signal from the caller's list — it warns and suggests, and
  every signal left out of a suggestion comes back with the reason.

---

## Layer 1 — Pure functions

`test_indicators.py` (20), `test_models_config.py` (72)

Indicators are pure functions over lists, so they are checked against values
computed by hand: an EMA seeded with its SMA, an RSI of exactly 100 on a
monotonic rise, a true range that widens across a gap. Every series is also
checked for **length alignment** — index `i` of any indicator must refer to
candle `i`, because a correct value at the wrong index is a look-ahead bug.

Config tests assert the loader rejects what it should: unknown keys (a typo must
not leave you trading a setting you think you changed), EMA periods out of
order, a `min_risk_reward` below 4.0, and a win-rate expressed as `85` rather
than `0.85`.

> **A note on fixtures.** One ADX test originally compared a trending series
> against a hand-built alternating one and failed — the alternating series has
> identical highs and lows on every bar, so it registers as *pure* direction and
> reads ADX 100. The fixture was wrong, not the indicator. Both series now come
> from the generator. When a test fails, the fixture is a suspect too.

## Layer 2 — Look-ahead guards

`test_structure.py` (27)

The load-bearing tests. A swing point at bar `i` is only knowable at bar
`i + right`, once the bars defining it have closed. Three tests attack that from
different angles:

- every pivot's `confirmed_at` strictly exceeds its own index, at several window sizes;
- nothing in a view built at bar `i` references anything past bar `i`;
- **a view at bar 300 is identical whether or not bars 301+ exist** — this is the
  one that would catch an accidental full-series peek.

There is also a test asserting a monotonic series has *no* swing points. That
looks like a triviality and is not: it documents that a strategy finding nothing
in a straight line is correct behaviour.

## Layer 3 — The rules that are the product

`test_risk.py` (28)

The 1:4 floor is the thing the README promises, so it gets tests that fail loudly
if anyone relaxes it: config refuses `min_risk_reward` below 4.0 (and below
3.99), a setup whose target falls short is **rejected rather than re-cut to fit**,
and costs are charged so a 4.25 gross setup nets exactly 4.0.

Position sizing is checked to never round *up* — rounding lots up would breach
the risk cap — and pip value is verified for quote-currency accounts (EURUSD/USD
= $10/lot), base-currency accounts (USDJPY/USD = 1000/price), and crosses, which
must be flagged approximate rather than silently guessed.

## Layer 4 — Simulation realism

`test_backtest.py` (18)

Each test pins one pessimistic modelling choice. Remove any of them and the
reported win rate rises without the strategy improving:

- **a bar touching both stop and target is a loss** (long and short). OHLC cannot
  say which came first; for a 1:4 system this single assumption can swing the
  win rate by tens of points, so it is always resolved against us;
- fills happen on the **next bar's open**, never the signal bar's close;
- a gap through the stop fills at the open, so a loss **can exceed 1R**;
- positions never overlap, so one idea is not counted as twenty wins;
- identical inputs produce byte-identical trades.

One test asserts that a signal built to net 4.0R actually *returns* 4.0R in
simulation. That is a cross-check between two modules: if `risk.py` and
`backtest.py` ever disagree about costs, the R figure stops meaning what the
signal card promised.

## Layer 5 — Statistical honesty

`test_metrics.py` (35), `test_risk_analysis.py` (30)

Wilson intervals are checked against textbook values (85/100 → 76.7%–90.7%) and
the critical values against known z-scores (1.9600, 2.5758, 1.6449).

The quality gate gets the hardest tests, because it is what stops the project
claiming an 85% win rate it has not earned:

- ten straight wins → `INSUFFICIENT DATA`, not a pass;
- 90% over 30 trades → `UNPROVEN`, because the interval still reaches too low;
- 90% over 1000 trades → `MEETS TARGET`;
- **exactly 85.0% can never pass**, at any sample size, because half the interval
  lies below the line. If that test ever goes green, the gate has started reading
  the point estimate instead of the bound.

Sizing tests assert the defining property of Kelly — that growth is *maximised*
at Kelly and lower on both sides — and that overestimating the edge is punished
asymmetrically, which is the argument for sizing off the interval's lower bound.

## Layer 6 — Optimisation equivalence

`test_precompute.py` (7), `test_edge.py` (20),
`test_limits.py` (22)

The evaluation cache made the tool 91× faster. Speed is never a reason to change
results, so these tests are its licence: for **every bar** of a 900-bar series,
the cached and uncached paths must produce identical signals, identical
confluence scores, and identical structure views. If they diverge, the cache is
wrong and the slow path is the source of truth.

One test builds a cache on the full series and another on a truncated prefix and
asserts they describe bar 300 identically. That is what catches a filter keyed on
the wrong index — an order block discovered at bar 500 leaking into a decision at
bar 300.

## Layer 7 — Web and API

`test_web.py` (60)

API handlers are plain functions from a dict to a dict, so the whole API is
tested without opening a socket. Then a real loopback server covers what only
HTTP can get wrong:

- **path traversal** — `/../pyproject.toml`, `/../../etc/passwd`, and URL-encoded
  variants all refused;
- **auth** — bearer header and query token accepted, wrong token refused, and
  the UI itself gated, not just the API;
- **secrets** — `/api/settings` reports whether a provider key exists and is
  asserted never to contain its value;
- **body limits** — an oversized POST gets 413 rather than an unbounded allocation;
- **errors are JSON, never tracebacks**;
- every asset the service worker caches is asserted to actually exist, because a
  404 in that list breaks the offline install.

## Layer 8 — End to end

`test_cli.py` (43)

Every command run through the real entry point against real files: `scan`,
`backtest --split`, `calibrate`, `risk`, `journal --close`, `data --generate`,
`data --fetch` (which is asserted to fail cleanly with no key rather than reach the
network).
These catch wiring mistakes that unit tests cannot — a renamed argument, a broken
import, a command that no longer exists.

`test_journal_outcomes.py` (26) covers the live-results loop: realised R for
longs, shorts and partial exits; closing appends rather than rewrites (asserted
by checking the file still *starts with* its original content); double-closes
refused; and a close event that precedes its signal in the file still folding
correctly.

---

## Browser verification

Automated tests cover behaviour; the UI was also rendered in Chromium via
Playwright at desktop and mobile widths, in both themes, checking for console
errors and horizontal overflow. That pass caught two real defects:

1. **Charts squashed the candles.** Including a 4R target in the price range —
   which by construction sits far outside recent price — compressed every candle
   into an unreadable band. Scaling is now candles-first, with off-range levels
   pinned to the edge and marked `↗`.
2. **Level labels were distorted.** The chart stretches non-uniformly to fill its
   column, which warped SVG text. Prices moved to an HTML legend.

The no-data notice was rendered the same way before it shipped: desktop and mobile
widths, light and dark, with the pair list expanded, checking `scrollWidth` against
`innerWidth` at each size. The commands wrap rather than scroll for that reason — a
command you have to drag sideways to read is a command nobody runs from a phone.

A third was caught the same way when the Pairs view was added:

3. **A single winning trade wore a `TRADE IT` badge.** With one trade and no loser
   there is no *realised* ratio, so the chance baseline fell back to the planned one —
   near 11% for a distant target — which one win clears. The expectancy bound did not
   catch it either: `mean_interval` returned a zero-width interval `(mean, mean)` for a
   sample of one, which passes every lower-bound test in the codebase. It now returns
   an unbounded interval, and a verdict requires the configured minimum sample.
   `test_pairs.py::TestSmallSamples` and `test_metrics.py::TestMeanInterval` pin both
   halves.

None of the three would have been caught by a passing test suite. If you change the
UI, look at it.

---

## Continuous integration

`.github/workflows/ci.yml` runs on every push and pull request, on Python 3.11
and 3.12:

1. **Import with no third-party packages** — the core must stay dependency-free.
   This step fails the moment a dependency creeps in.
2. **Byte-compile** `src` and `tests`.
3. **Full test suite.**
4. **End-to-end smoke test** — `scan`, `backtest --split`, and `calibrate` against
   the bundled data, so a working suite with a broken CLI still fails.

---

## Writing new tests

From `CLAUDE.md`: any change to `indicators.py`, `structure.py`, `risk.py`, or
`backtest.py` ships with a test. The R:R floor and the look-ahead guards have
dedicated tests — if you change that behaviour, the test must be updated
deliberately and the commit must say why.

Three rules for fixtures:

- **No network.** Use `data/synthetic.py`; it is seeded and offline.
- **Deterministic.** Same input, same output, every run. A wobbling test is a
  broken test.
- **Realistic.** Degenerate fixtures produce degenerate conclusions — see the ADX
  note above.

And the one that matters most: when a test fails, establish whether the code or
the fixture is wrong **before** changing either. Two of the failures found while
building this suite were bad fixtures, and one was the system correctly reporting
an outcome the test had not anticipated.

---

## The ledger, and the datafeed reader

`test_ledger.py` (41), `test_dukascopy.py` (26)

The ledger is where "what the model predicted" meets "what happened", so its tests
guard the seam between the two. A replay must reproduce `run_backtest` trade for trade,
and must leave the journal untouched — asserted directly, because the moment a replayed
outcome can reach the forward record the record means nothing. A settled prediction must
carry the simulator's own R, measured from the cost-adjusted fill rather than the plan,
and the bars from fill to exit, so that "what happened" is a record and not a
recollection. The snapshot journalled with a signal lists every check, fired or not, and
comes back through the journal intact; the deadlines on a recorded claim are read back
rather than recomputed, so a later change to the horizon cannot move the goalposts.
The open-call advice is checked for saying both halves — *if you are in* and *if you
are not* — because the tool never placed the order and cannot know which is true.

The Dukascopy reader never sees the network in the suite. Its tests build `.bi5` files
the way the datafeed serves them (fixed 24-byte records under LZMA), then check the
decoded timestamps and prices, that the flat zero-volume hours of a closed market are
dropped, that the 10^3 versus 10^5 scale is chosen from the instrument's digits and
corrected by price magnitude when a three-decimal exotic is mislabelled, and that a
currency which has moved thirtyfold is still treated as history rather than as a
decimal error. The month walk is driven through a fake fetcher: it skips a current month
with no file yet, stitches older months newest-first, counts only tradable hours toward
what was asked for, resamples hourly files to H4 and minute files to M15, and gives up
in a bounded number of requests on a symbol the feed does not carry.
