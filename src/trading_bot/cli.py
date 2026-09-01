"""Command line interface.

``scan`` is the product; everything else exists to tell you whether ``scan``
is worth listening to.
"""

from __future__ import annotations

import argparse
import sys
import textwrap
from datetime import datetime
from pathlib import Path

from . import __version__
from . import exposure
from .backtest import run_backtest, split_backtest
from .calibrate import format_ceiling_sweep, format_sweep, sweep, sweep_ceiling
from .clock import humanise_delta
from .config import Config, load_config
from .data.base import missing_symbols
from .data.csv_source import CsvSource, fill_commands, fill_directory, load_csv
from .data.synthetic import SyntheticSource, generate
from .errors import DataError, TradingBotError
from .forecast import (
    bars_to_time,
    build_prediction,
    measure_base_rate,
    resolve_open_predictions,
    scoreboard,
)
from .instruments import expand_symbols, get_instrument, group_names
from .journal import Journal
from .ledger import (
    ORIGIN_FORWARD,
    ORIGIN_REPLAY,
    format_case,
    format_live,
    format_scorecards,
    format_table,
    live_status,
    load_cases,
    replay,
    snapshot,
    summarise,
)
from .limits import evaluate_limits
from .metrics import compute_metrics
from .models import Timeframe, utc_now
from .pairs import (
    analyse_universe,
    format_persistence,
    format_universe,
    persistence_check,
)
from .playbook import daily_briefing, explain_no_signal
from .report import format_comparison, format_result, format_trades
from .risk_analysis import analyse, analyse_from_metrics, format_report
from .scanner import scan_latest
from .signals import format_signal, format_signal_compact, no_signal_message
from .web.server import TOKEN_ENV

BANNER = "trading.bot - forex advisor. It tells you what to do; you place the trade."


def build_source(config: Config, override: str | None = None):
    """Pick a data source from config, with a CLI override."""
    kind = (override or config.data.source).lower()
    if kind == "csv":
        return CsvSource(config.data.csv_dir)
    if kind == "synthetic":
        return SyntheticSource()
    if kind == "rest":
        from .data.rest_source import build_rest_source

        return build_rest_source(config.data.provider, config.data.api_key)
    raise TradingBotError(f"unknown data source {kind!r}")


def data_gap_lines(
    gaps: list[str], timeframe: Timeframe, directory: Path | str,
    indent: str = "  ", names: bool = True,
) -> list[str]:
    """The one paragraph to print about symbols with no file behind them.

    Printed once with the whole list, not once per symbol. Sixty repetitions of
    the same sentence is how a fixable setup problem reads as a broken program,
    and it buries the pairs that *were* scanned underneath it.

    ``names`` is off for callers whose own report already lists the pairs, so
    the reader gets the remedy without reading the same sixty symbols twice.
    """
    if not gaps:
        return []
    lines = [""]
    if names:
        lines += [
            f"{indent}{len(gaps)} instrument(s) have no {timeframe.name} data in "
            f"{directory}, so they were not judged at all.",
            f"{indent}Unmeasured is not the same as no setup:",
        ]
        lines += [f"{indent}  {chunk}" for chunk in textwrap.wrap(", ".join(gaps), width=72)]
    lines.append(f"{indent}Fill them in one command:")
    for command, note in fill_commands(timeframe, only_missing=True):
        lines += [f"{indent}  {command}", f"{indent}    {note}"]
    return lines


def cmd_scan(args, config: Config) -> int:
    """Evaluate the latest closed bar for each symbol and say what to do.

    The output is the product, so it is deliberately verbose: a briefing in the
    reader's own timezone, a full card per setup with the prediction it implies,
    an explanation of the near misses, and the currency exposure the whole lot
    would create. ``--brief`` cuts it back for someone who has read it before.
    """
    source = build_source(config, args.source)
    timeframe = Timeframe.parse(args.timeframe or config.data.timeframe)
    symbols = expand_symbols(args.symbols) if args.symbols else config.data.resolved_symbols
    journal = Journal(config.journal_path) if not args.no_journal else None
    clock = config.clock
    detail = "brief" if (args.brief or args.compact) else config.display.detail
    now = utc_now()

    print(BANNER)
    print()
    for line in daily_briefing(config, clock, now):
        print(line)
    print()
    print(f"  Scanning {len(symbols)} instrument(s) on {timeframe.name} - "
          f"min confluence {config.strategy.min_confluence:.0%}, "
          f"min R:R {config.risk.min_risk_reward:.0f}:1")
    print()

    status = evaluate_limits(Journal(config.journal_path).read(), config)
    if status.breached:
        print(status.banner())
        print()

    signals: list = []
    predictions: dict = {}
    base_rates: dict = {}
    evaluations: dict = {}
    near_misses: list = []
    unavailable: list = []

    for symbol in symbols:
        try:
            candles = source.fetch(symbol, timeframe, config.data.lookback_bars)
            evaluation = scan_latest(candles, symbol, config)
        except TradingBotError as exc:
            unavailable.append((symbol, str(exc)))
            continue

        if not evaluation.has_signal:
            near_misses.append((symbol, evaluation))
            continue

        signal = evaluation.signal
        signals.append(signal)
        evaluations[symbol] = evaluation
        # Measure the base rate only for pairs that actually signalled: it costs
        # a backtest per symbol, and a rate nobody will read is a rate not worth
        # computing.
        if not args.no_base_rate:
            base_rates[symbol] = measure_base_rate(candles, symbol, config)
            predictions[symbol] = build_prediction(signal, config, base_rates[symbol])

    # ------------------------------------------------------------- the setups
    if signals:
        print("=" * 78)
        print(f"  {len(signals)} SETUP(S) FOUND")
        print("=" * 78)
        for signal in signals:
            instrument = get_instrument(signal.symbol)
            print()
            if args.compact:
                print(format_signal_compact(signal, instrument))
            else:
                print(format_signal(
                    signal, instrument, config, clock,
                    prediction=predictions.get(signal.symbol), detail=detail,
                ))
            print()
            if journal is not None:
                # The snapshot goes on the same line as the signal, so the ledger
                # can later ask not just "was it right" but "was it right for
                # the reasons it gave".
                journal.record_once(signal, context=snapshot(
                    evaluations[signal.symbol], config, predictions.get(signal.symbol)
                ))

    # ---------------------------------------------------------- what was close
    ranked = sorted(
        near_misses, key=lambda pair: pair[1].confluence_fraction or 0.0, reverse=True
    )
    explained = (
        [n for n in ranked if (n[1].confluence_fraction or 0) >= 0.5][: args.explain]
        if detail == "full"
        else []
    )
    explained_symbols = {symbol for symbol, _ in explained}

    if explained:
        print("-" * 78)
        print(f"  CLOSEST TO A SETUP  ({len(explained)} of {len(near_misses)} pairs "
              f"with no trade)")
        print("-" * 78)
        for symbol, evaluation in explained:
            print()
            for line in explain_no_signal(
                symbol, timeframe.name, evaluation.confluence_fraction,
                evaluation.confluence, evaluation.direction, config,
                get_instrument(symbol), clock,
            ):
                print(line)
        print()

    # Every remaining pair still gets a line. A pair that silently vanished from
    # the output would be indistinguishable from one that was never scanned.
    rest = [(sym, ev) for sym, ev in ranked if sym not in explained_symbols]
    if rest:
        if explained:
            print("-" * 78)
            print("  NOTHING HERE  (scanned, scored, nowhere near)")
            print("-" * 78)
        for symbol, evaluation in rest:
            print(f"  {no_signal_message(symbol, timeframe.name, evaluation.confluence_fraction)}")
        print()

    # ------------------------------------------------------------- the exposure
    if len(signals) > 1:
        report = exposure.analyse(signals, config, base_rates)
        print(exposure.format_exposure(report, config))
        print()

    # ---------------------------------------------------------------- the tally
    gaps = missing_symbols(source, symbols, timeframe)
    gapped = set(gaps)
    broken = [(sym, msg) for sym, msg in unavailable if sym not in gapped]

    print("=" * 78)
    print(f"  {len(signals)} setup(s) found across {len(symbols) - len(unavailable)} "
          f"instrument(s) scanned. {len(near_misses)} had none.")
    if signals and journal is not None:
        print(f"  Recorded as predictions in {journal.path} — settle and review them with:")
        print("    python -m trading_bot ledger --resolve")
    if not signals:
        print("  Nothing met the rules. No setup is a position too.")
    # What could not be looked at goes last: it is a footnote about the data,
    # not an answer about the market, and it must not push the answer off screen.
    for symbol, message in broken:
        print(f"  {symbol} could not be read: {message}")
    for line in data_gap_lines(gaps, timeframe, getattr(source, "directory", "the data directory")):
        print(line)
    print("=" * 78)
    return 0


def cmd_pairs(args, config: Config) -> int:
    """Measure the win rate pair by pair, and say which pairs are worth trading."""
    source = build_source(config, args.source)
    timeframe = Timeframe.parse(args.timeframe or config.data.timeframe)
    symbols = expand_symbols(args.symbols) if args.symbols else config.data.resolved_symbols
    bars = args.bars or max(config.data.lookback_bars, 1500)

    print(BANNER)
    print(f"\nMeasuring {len(symbols)} instrument(s) on {timeframe.name}, "
          f"{bars} bars each. This walks every bar of history, so it takes a moment.\n")

    candles_by_symbol: dict[str, list] = {}
    for symbol in symbols:
        try:
            candles_by_symbol[symbol.upper()] = source.fetch(symbol, timeframe, bars)
        except TradingBotError:
            continue

    split = None if args.split in (0, None) else args.split
    report = analyse_universe(candles_by_symbol, config, split=split, symbols=symbols)
    print(format_universe(report, config))
    for line in data_gap_lines(
        missing_symbols(source, symbols, timeframe), timeframe,
        getattr(source, "directory", "the data directory"), indent="    ", names=False,
    ):
        print(line)

    if args.persistence:
        print()
        print(format_persistence(persistence_check(candles_by_symbol, config)))
    else:
        print()
        print("  Before you act on the table above, test whether picking pairs on past")
        print("  results carries forward at all:  python -m trading_bot pairs --persistence")
    return 0


def cmd_forecast(args, config: Config) -> int:
    """Show, settle and score the predictions this tool has made.

    This is the honest scoreboard, and it is deliberately separate from the
    backtest: a backtest replays trades whose outcome was already in the file,
    and cannot contribute a single entry here.
    """
    journal = Journal(args.path or config.journal_path)
    clock = config.clock
    now = utc_now()

    print(BANNER)
    print()

    if args.resolve:
        source = build_source(config, args.source)
        print("Settling open predictions against real candles, "
              "by the same rules the backtest uses...\n")
        reports = resolve_open_predictions(journal, source, config)
        if not reports:
            print("  No open predictions to settle.")
        for item in reports:
            if item["status"] == "resolved":
                print(f"  {item['symbol']:<8} {item['outcome']:<8} "
                      f"{item['r_multiple']:+.2f}R   {item['detail']}")
            else:
                print(f"  {item['symbol']:<8} {item['status']:<8}          {item['detail']}")
        print()

    open_entries = journal.open_entries()
    if open_entries:
        print("-" * 78)
        print(f"  LIVE PREDICTIONS  ({len(open_entries)} awaiting an answer)")
        print("-" * 78)
        for entry in open_entries[-20:]:
            sig = entry.signal
            issued = datetime.fromisoformat(entry.issued_at)
            deadline = bars_to_time(
                issued, config.backtest.max_bars_in_trade,
                Timeframe.parse(sig.get("timeframe", config.data.timeframe)),
            )
            state = "EXPIRED" if now > deadline else humanise_delta(deadline - now)
            print(f"  {sig.get('symbol', '?'):<8} {sig.get('direction', '?'):<5} "
                  f"entry {sig.get('entry')}  tp {sig.get('take_profit')}  "
                  f"sl {sig.get('stop_loss')}")
            print(f"           made {clock.stamp(issued)}  -  resolves by "
                  f"{clock.stamp(deadline)} ({state})")
        print()

    board = scoreboard(journal, config.target.confidence)
    print("=" * 78)
    print("  FORWARD RECORD  (predictions made before the outcome was knowable)")
    print("=" * 78)
    for line in board.summary(clock):
        print(f"  {line}")
    print()
    print("  A backtest cannot add a single trade to this number. That is the point of it.")
    print("=" * 78)
    return 0


def _print_settlements(reports: list[dict]) -> None:
    """One line per open prediction examined by the resolver."""
    if not reports:
        print("  No open predictions to settle.")
    for item in reports:
        if item["status"] == "resolved":
            print(f"  {item['symbol']:<8} {item['outcome']:<8} "
                  f"{item['r_multiple']:+.2f}R   {item['detail']}")
        else:
            print(f"  {item['symbol']:<8} {item['status']:<8}          {item['detail']}")
    print()


def _live_reports(cases: list, config: Config, source) -> list:
    """Judge every open case against the freshest candles the source has.

    Candles are fetched once per symbol and timeframe: sixty open predictions
    on three pairs is three requests, not sixty.
    """
    cache: dict = {}
    reports = []
    for case in cases:
        key = (case.symbol, case.signal.timeframe)
        if key not in cache:
            try:
                cache[key] = source.fetch(case.symbol, case.signal.timeframe,
                                          config.data.lookback_bars)
            except TradingBotError as exc:
                cache[key] = exc
        candles = cache[key]
        if isinstance(candles, Exception):
            reports.append((case, None, str(candles)))
            continue
        reports.append((case, live_status(case, candles, config), ""))
    return reports


def _print_ledger(cases: list, origin: str, config: Config, args, heading: list[str]) -> None:
    """The ledger in full: the record, the table, the scorecards, the case files."""
    clock = config.clock
    width = 78
    print("=" * width)
    for line in heading:
        print(f"  {line}")
    print("=" * width)
    summary = summarise(cases, config, origin)
    for line in summary.lines(clock):
        print(f"  {line}")
    print()

    if not cases:
        return

    print("-" * width)
    print("  EVERY CALL, NEWEST FIRST")
    print("-" * width)
    for line in format_table(cases, clock):
        print(line)
    print()

    if not args.brief:
        print("-" * width)
        print("  SCORECARDS  (resolved calls only; n beside every rate)")
        print("-" * width)
        for line in format_scorecards(cases, config):
            print(line)
        print()

        shown = cases if args.all else cases[-args.limit:]
        print("-" * width)
        print(f"  CASE FILES  ({len(shown)} of {len(cases)}, newest first"
              f"{'' if args.all else '; --all for every one, --id for one'})")
        print("-" * width)
        first_number = len(cases) - len(shown) + 1
        for offset, case in enumerate(reversed(shown)):
            number = first_number + len(shown) - 1 - offset
            print()
            for line in format_case(case, clock, config, number=number):
                print(line)
        print()


def _export_cases(path: str, cases: list, origin: str, config: Config) -> None:
    import json

    clock = config.clock
    payload = {
        "origin": origin,
        "exported_at": utc_now().isoformat(),
        "summary": summarise(cases, config, origin).to_dict(clock),
        "cases": [case.to_dict(clock) for case in cases],
    }
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(payload, indent=1, sort_keys=True), encoding="utf-8")
    print(f"  Wrote {len(cases)} case file(s) to {path}")


def cmd_ledger(args, config: Config) -> int:
    """Every prediction the model made, what it saw, and what happened next.

    Two ledgers, never mixed. The forward ledger is the journal: calls written
    down before their outcome existed, then settled by the backtest's own
    resolver. A replay walks a history and shows what the model would have
    said and what followed, labelled on every line as the replay it is.
    """
    clock = config.clock
    print(BANNER)
    print()

    if args.replay:
        candles = _load_candles(args, config)
        if args.symbol:
            symbol = args.symbol.upper()
        elif args.csv:
            symbol = Path(args.csv).stem.split("_")[0].upper()
        else:
            symbol = config.data.resolved_symbols[0]
        split = None if args.split in (0, None) else args.split
        start = int(len(candles) * split) if split else None
        cases = replay(candles, symbol, config, start=start)
        window = (
            f"{clock.day(candles[start or 0].timestamp)} to {clock.day(candles[-1].timestamp)}"
        )
        heading = [
            f"REPLAY  -  {symbol} {config.data.timeframe}, {len(candles) - (start or 0)} bars, "
            f"{window}" + (f"  (out-of-sample tail, split {split})" if split else ""),
            "What the model would have said at every bar, and what happened after.",
            "The outcomes were in the file before the calls were made: this is a",
            "backtest wearing a diary, not a track record. The forward ledger is.",
        ]
        _print_ledger(cases, ORIGIN_REPLAY, config, args, heading)
        if args.export:
            _export_cases(args.export, cases, ORIGIN_REPLAY, config)
        return 0

    journal = Journal(args.path or config.journal_path)
    if args.resolve:
        source = build_source(config, args.source)
        print("Settling open predictions against real candles, by the backtest's rules...\n")
        _print_settlements(resolve_open_predictions(journal, source, config))

    cases = load_cases(journal, config)

    if args.id:
        wanted = args.id.strip().upper()
        match = next((c for c in cases if c.id.upper() == wanted), None)
        if match is None:
            known = ", ".join(c.id for c in cases[-5:]) or "none"
            print(f"error: no prediction with id {args.id!r}. Recent ids: {known}",
                  file=sys.stderr)
            return 2
        if match.is_open:
            source = build_source(config, args.source)
            for case, status, problem in _live_reports([match], config, source):
                print("  WHERE IT STANDS NOW")
                if status is None:
                    print(f"    could not judge it: {problem}")
                else:
                    for line in format_live(status, clock, config):
                        print(line)
                print()
        for line in format_case(match, clock, config, number=cases.index(match) + 1):
            print(line)
        return 0

    open_cases = [c for c in cases if c.is_open]
    if open_cases:
        source = build_source(config, args.source)
        print("-" * 78)
        print(f"  OPEN PREDICTIONS  ({len(open_cases)}) - where each stands, and what to do now")
        print("-" * 78)
        for case, status, problem in _live_reports(open_cases, config, source):
            print()
            if status is None:
                print(f"  {case.signal.direction.value.upper():<5} {case.symbol}  "
                      f"made {clock.stamp(case.made_at)}")
                print(f"    could not judge it: {problem}")
                continue
            for line in format_live(status, clock, config):
                print(line)
        print()
    if args.open:
        if not open_cases:
            print("  No open predictions.")
        return 0

    heading = [
        "FORWARD LEDGER  -  every prediction written down before its outcome existed",
        f"journal: {journal.path}",
    ]
    _print_ledger(cases, ORIGIN_FORWARD, config, args, heading)
    if not cases:
        print("  Run a scan with journalling on, and come back after the market has answered:")
        print("    python -m trading_bot scan")
        print("    python -m trading_bot ledger --resolve")
    if args.export:
        _export_cases(args.export, cases, ORIGIN_FORWARD, config)
    return 0


def cmd_backtest(args, config: Config) -> int:
    """Measure the strategy on history."""
    candles = _load_candles(args, config)
    symbol = args.symbol or config.data.resolved_symbols[0]

    print(BANNER)
    print(f"Backtesting {symbol} on {len(candles)} bars\n")

    if args.split:
        in_sample, out_sample = split_backtest(candles, symbol, config, args.split)
        print(format_comparison(in_sample, out_sample, config))
        if args.trades:
            print("\nOut-of-sample trades:\n")
            print(format_trades(out_sample))
    else:
        result = run_backtest(candles, symbol, config)
        print(format_result(result, config))
        if args.trades:
            print()
            print(format_trades(result))
    return 0


def cmd_calibrate(args, config: Config) -> int:
    """Sweep the confluence threshold to expose the selectivity trade-off."""
    candles = _load_candles(args, config)
    symbol = args.symbol or config.data.resolved_symbols[0]

    print(BANNER)
    print(f"Calibrating {symbol} on {len(candles)} bars "
          f"({'out-of-sample only' if args.split else 'full series'})\n")
    if args.ceiling:
        print(format_ceiling_sweep(
            sweep_ceiling(candles, symbol, config, split=args.split), symbol
        ))
    else:
        print(format_sweep(sweep(candles, symbol, config, split=args.split)))
        print()
        print("The confluence dial decides which setups are taken. How far the target may")
        print("sit is a separate dial, and on some data it matters more:")
        print("  python -m trading_bot calibrate --ceiling")
    return 0


def cmd_journal(args, config: Config) -> int:
    """Show what has been advised, or record how a trade finished."""
    journal = Journal(args.path or config.journal_path)

    if args.close:
        if args.exit is None:
            print("error: --close needs --exit <price>", file=sys.stderr)
            return 2
        entry = journal.close(args.close, args.exit, note=args.note or "")
        print(f"closed {entry.entry_id}")
        print(f"  exit      {entry.exit_price}")
        print(f"  outcome   {entry.outcome}")
        print(f"  result    {entry.r_multiple:+.2f}R")
        return 0

    if args.open:
        entries = journal.open_entries()
        if not entries:
            print("No open signals.")
            return 0
        print(f"{len(entries)} open signal(s):\n")
        for entry in entries:
            sig = entry.signal
            print(f"  {entry.entry_id}")
            print(f"    {sig.get('direction')} {sig.get('symbol')} "
                  f"entry {sig.get('entry')} sl {sig.get('stop_loss')} tp {sig.get('take_profit')}")
        print("\nClose one with: trading-bot journal --close <id> --exit <price>")
        return 0

    print(journal.summary())
    return 0


def cmd_risk(args, config: Config) -> int:
    """Show how much to risk per trade, and what it costs to be wrong."""
    print(BANNER)
    if args.from_backtest:
        candles = _load_candles(args, config)
        symbol = args.symbol or config.data.resolved_symbols[0]
        result = run_backtest(candles, symbol, config)
        metrics = compute_metrics(result.trades, config.target.confidence)
        if metrics.is_empty:
            print(f"error: the backtest on {symbol} produced no trades to size from",
                  file=sys.stderr)
            return 2
        report = analyse_from_metrics(
            metrics, config.risk.min_risk_reward, args.trades, args.trials,
            config.target.confidence
        )
    else:
        report = analyse(args.win_rate, config.risk.min_risk_reward, args.trades, args.trials)
    print(format_report(report, config.account.balance, config.account.currency))
    return 0


def cmd_serve(args, config: Config) -> int:
    """Run the web UI, reachable from a browser on any device."""
    from .web.server import serve

    serve(config, host=args.host, port=args.port, token=args.token, open_browser=args.open)
    return 0


def _humanise_seconds(seconds: float) -> str:
    """'40s', '9 min': a wait a person can decide whether to sit through."""
    return f"{seconds:.0f}s" if seconds < 90 else f"{seconds / 60:.0f} min"


def _fill_files(source, symbols, timeframe, out_dir: Path, args, pause: float) -> int:
    """Run one fill pass, printing each symbol as it lands.

    ``pause`` is the gap between provider requests. Twelve Data's free tier
    allows eight calls a minute and the whole registry is sixty-four of them, so
    the default is slow on purpose: finishing in nine minutes beats being cut
    off after the first eight pairs.
    """
    written: list[str] = []
    skipped: list[str] = []
    failed: list[tuple[str, str]] = []

    for result in fill_directory(
        source, symbols, timeframe, out_dir, args.bars,
        only_missing=args.only_missing, pause=pause,
    ):
        if result.status == "written":
            written.append(result.symbol)
            print(f"wrote {result.bars} bars to {result.path}")
        elif result.status == "skipped":
            skipped.append(result.symbol)
            print(f"skipped {result.symbol} ({result.message})")
        else:
            failed.append((result.symbol, result.message))
            print(f"FAILED {result.symbol}: {result.message}")

    print()
    print(f"{len(written)} written, {len(skipped)} already present, {len(failed)} failed.")
    if failed:
        print("  Failed pairs are still missing and will report no data when you scan:")
        for symbol, message in failed[:5]:
            print(f"    {symbol}: {message}")
        if len(failed) > 5:
            print(f"    and {len(failed) - 5} more")
    # A run where nothing at all landed is a failed run, whatever the exit code
    # of the individual requests said.
    return 0 if written or skipped else 1


def cmd_data(args, config: Config) -> int:
    """Create or inspect candle files."""
    out_dir = Path(args.out or config.data.csv_dir)
    timeframe = Timeframe.parse(args.timeframe or config.data.timeframe)
    symbols = expand_symbols(args.symbols or config.data.symbols)

    if args.fetch:
        try:
            source = build_source(config, "rest")
        except TradingBotError as exc:
            # The dead end this command exists to get someone out of. Say the
            # offline alternative here rather than leaving them to find it.
            raise DataError(
                f"{exc} To fill the directory without a key, synthetic bars for "
                f"testing the pipeline: python -m trading_bot data --generate."
            ) from exc
        pending = len(CsvSource(out_dir).missing(symbols, timeframe)) if args.only_missing \
            else len(symbols)
        provider = config.data.provider.strip().lower()
        pause = args.pause if args.pause is not None else (
            8.0 if provider in ("twelvedata", "twelve_data", "td") else 0.0
        )
        print(f"Fetching {pending} of {len(symbols)} symbol(s) at {timeframe.name} "
              f"from {config.data.provider} into {out_dir}")
        if pause > 0 and pending > 1:
            print(f"  Pausing {pause:g}s between requests for the provider's rate "
                  f"limit, about {_humanise_seconds(pause * (pending - 1))} in total. "
                  f"Pass --pause 0 if your plan allows more.")
        print()
        return _fill_files(source, symbols, timeframe, out_dir, args, pause=pause)

    if args.generate:
        code = _fill_files(
            SyntheticSource(seed=args.seed), symbols, timeframe, out_dir, args, pause=0.0
        )
        print("\nThis is synthetic data for testing the pipeline.")
        print("It is not a market. Never quote results from it as performance.")
        return code

    if args.inspect:
        candles = load_csv(args.inspect)
        print(f"{args.inspect}: {len(candles)} bars")
        print(f"  from {candles[0].timestamp.isoformat()}")
        print(f"  to   {candles[-1].timestamp.isoformat()}")
        lo = min(c.low for c in candles)
        hi = max(c.high for c in candles)
        print(f"  range {lo:.5f} - {hi:.5f}")
        return 0

    print("Nothing to do. Pass --fetch, --generate or --inspect PATH.")
    return 1


def _load_candles(args, config: Config):
    """Resolve candles for backtest/calibrate from the flags given."""
    if args.csv:
        return load_csv(args.csv)
    symbol = args.symbol or config.data.resolved_symbols[0]
    timeframe = Timeframe.parse(args.timeframe or config.data.timeframe)
    source = build_source(config, args.source)
    return source.fetch(symbol, timeframe, args.bars or config.data.lookback_bars)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="trading_bot",
        description=BANNER,
        epilog="This tool never places an order. That is deliberate.",
    )
    parser.add_argument("--version", action="version", version=f"trading.bot {__version__}")
    parser.add_argument("--config", help="path to a TOML config file")
    sub = parser.add_subparsers(dest="command", required=True)

    groups = ", ".join(group_names())
    scan = sub.add_parser("scan", help="show what to do right now")
    scan.add_argument("--symbols", nargs="+",
                      help=f"symbols or group names to scan ({groups})")
    scan.add_argument("--timeframe", help="override the configured timeframe")
    scan.add_argument("--source", choices=["csv", "rest", "synthetic"])
    scan.add_argument("--compact", action="store_true", help="one line per signal")
    scan.add_argument("--brief", action="store_true",
                      help="drop the step-by-step coaching from each card")
    scan.add_argument("--explain", type=int, default=3, metavar="N",
                      help="how many near-miss pairs to explain in full (default 3)")
    scan.add_argument("--no-base-rate", action="store_true", dest="no_base_rate",
                      help="skip the per-pair base-rate measurement (faster)")
    scan.add_argument("--no-journal", action="store_true", help="do not record signals")
    scan.set_defaults(func=cmd_scan)

    prs = sub.add_parser("pairs", help="win rate pair by pair, and which to trade")
    prs.add_argument("--symbols", nargs="+",
                     help=f"symbols or group names to measure ({groups})")
    prs.add_argument("--timeframe")
    prs.add_argument("--source", choices=["csv", "rest", "synthetic"])
    prs.add_argument("--bars", type=int, help="bars of history per pair")
    prs.add_argument("--split", type=float, default=0.7,
                     help="measure on the out-of-sample tail (default 0.7); 0 for full series")
    prs.add_argument("--persistence", action="store_true",
                     help="walk-forward test of whether picking pairs helps at all")
    prs.set_defaults(func=cmd_pairs)

    fc = sub.add_parser("forecast", help="live predictions and the forward scoreboard")
    fc.add_argument("--resolve", action="store_true",
                    help="settle open predictions against real candles")
    fc.add_argument("--source", choices=["csv", "rest", "synthetic"])
    fc.add_argument("--path", help="journal file")
    fc.set_defaults(func=cmd_forecast)

    lg = sub.add_parser("ledger",
                        help="every prediction, what the model saw, and what happened")
    lg.add_argument("--resolve", action="store_true",
                    help="settle open predictions against real candles first")
    lg.add_argument("--open", action="store_true",
                    help="only the open predictions: where each stands and what to do now")
    lg.add_argument("--id", metavar="ID", help="one prediction in full, e.g. EURUSD@2024-...")
    lg.add_argument("--replay", action="store_true",
                    help="walk a history instead: what the model would have said, and what "
                         "followed (a backtest wearing a diary, labelled as such)")
    lg.add_argument("--csv", help="history for --replay")
    lg.add_argument("--symbol", help="symbol for --replay (inferred from the CSV name)")
    lg.add_argument("--timeframe")
    lg.add_argument("--source", choices=["csv", "rest", "synthetic"])
    lg.add_argument("--bars", type=int, help="bars of history to replay")
    lg.add_argument("--split", type=float, default=None, metavar="F",
                    help="replay only the out-of-sample tail after this fraction (e.g. 0.7)")
    lg.add_argument("--limit", type=int, default=10, metavar="N",
                    help="how many case files to print in full (default 10, newest first)")
    lg.add_argument("--all", action="store_true", help="print every case file in full")
    lg.add_argument("--brief", action="store_true",
                    help="the record and the table only; no scorecards or case files")
    lg.add_argument("--export", metavar="PATH", help="also write every case file as JSON")
    lg.add_argument("--path", help="journal file")
    lg.set_defaults(func=cmd_ledger)

    back = sub.add_parser("backtest", help="measure the strategy on history")
    back.add_argument("--csv", help="path to an OHLCV CSV")
    back.add_argument("--symbol")
    back.add_argument("--timeframe")
    back.add_argument("--source", choices=["csv", "rest", "synthetic"])
    back.add_argument("--bars", type=int, help="how many bars to load")
    back.add_argument("--split", type=float, metavar="F",
                      help="fraction for in-sample; the rest is out-of-sample (e.g. 0.7)")
    back.add_argument("--trades", action="store_true", help="list individual trades")
    back.set_defaults(func=cmd_backtest)

    cal = sub.add_parser("calibrate", help="sweep the confluence threshold")
    cal.add_argument("--csv")
    cal.add_argument("--symbol")
    cal.add_argument("--timeframe")
    cal.add_argument("--source", choices=["csv", "rest", "synthetic"])
    cal.add_argument("--bars", type=int)
    cal.add_argument("--split", type=float, default=0.7,
                     help="measure each threshold out-of-sample (default 0.7); 0 for full series")
    cal.add_argument("--ceiling", action="store_true",
                     help="sweep the reward ceiling instead of the confluence threshold")
    cal.set_defaults(func=cmd_calibrate)

    jour = sub.add_parser("journal", help="show journalled signals, or close one")
    jour.add_argument("--path", help="journal file")
    jour.add_argument("--list", action="store_true", help="list entries (default)")
    jour.add_argument("--open", action="store_true", help="list only signals still open")
    jour.add_argument("--close", metavar="ID", help="record the outcome of a signal")
    jour.add_argument("--exit", type=float, metavar="PRICE", help="price you actually exited at")
    jour.add_argument("--note", help="optional note to store with the outcome")
    jour.set_defaults(func=cmd_journal)

    risk = sub.add_parser("risk", help="how much to risk per trade")
    risk.add_argument("--win-rate", type=float, default=0.30, dest="win_rate",
                      help="win rate as a fraction (default 0.30)")
    risk.add_argument("--from-backtest", action="store_true", dest="from_backtest",
                      help="measure the win rate from a backtest instead of assuming one")
    risk.add_argument("--trades", type=int, default=60, help="trades per period (default 60)")
    risk.add_argument("--trials", type=int, default=5000, help="Monte Carlo trials")
    risk.add_argument("--csv")
    risk.add_argument("--symbol")
    risk.add_argument("--timeframe")
    risk.add_argument("--source", choices=["csv", "rest", "synthetic"])
    risk.add_argument("--bars", type=int)
    risk.set_defaults(func=cmd_risk)

    web = sub.add_parser("serve", help="run the web UI (open it from any device)")
    web.add_argument("--host", default="127.0.0.1",
                     help="0.0.0.0 to allow other devices on your network (default 127.0.0.1)")
    web.add_argument("--port", type=int, default=8787)
    web.add_argument("--token", help=f"access token; also read from ${TOKEN_ENV}")
    web.add_argument("--open", action="store_true", help="open a browser on start")
    web.set_defaults(func=cmd_serve)

    data = sub.add_parser("data", help="create or inspect candle files")
    data.add_argument("--fetch", action="store_true",
                      help="download real candles from the configured provider "
                           "into the CSV directory (needs a provider key)")
    data.add_argument("--generate", action="store_true", help="write synthetic sample CSVs")
    data.add_argument("--inspect", metavar="PATH", help="summarise a CSV")
    data.add_argument("--symbols", nargs="+",
                      help=f"symbols or group names ({groups}); defaults to the config")
    data.add_argument("--timeframe")
    data.add_argument("--bars", type=int, default=2000)
    data.add_argument("--seed", type=int, default=42)
    data.add_argument("--only-missing", action="store_true",
                      help="leave symbols that already have a file alone")
    data.add_argument("--pause", type=float, default=None,
                      help="seconds between provider requests. Default: 8 for Twelve Data, "
                           "whose free tier allows about 8 a minute; 0 for Dukascopy, "
                           "which paces itself")
    data.add_argument("--out", help="output directory")
    data.set_defaults(func=cmd_data)

    return parser


def main(argv: list[str] | None = None) -> int:
    """Entry point. Domain errors print a message; they do not dump a traceback."""
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
        if getattr(args, "split", None) == 0:
            args.split = None
        return args.func(args, config)
    except TradingBotError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
