"""Command line interface.

``scan`` is the product; everything else exists to tell you whether ``scan``
is worth listening to.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__
from .backtest import run_backtest, split_backtest
from .calibrate import format_sweep, sweep
from .config import Config, load_config
from .data.csv_source import CsvSource, load_csv, write_csv
from .data.synthetic import SyntheticSource, generate
from .errors import TradingBotError
from .instruments import get_instrument
from .journal import Journal
from .models import Timeframe
from .report import format_comparison, format_result, format_trades
from .scanner import scan_latest
from .signals import format_signal, format_signal_compact, no_signal_message

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


def cmd_scan(args, config: Config) -> int:
    """Evaluate the latest closed bar for each symbol and say what to do."""
    source = build_source(config, args.source)
    timeframe = Timeframe.parse(args.timeframe or config.data.timeframe)
    symbols = args.symbols or config.data.symbols
    journal = Journal(config.journal_path) if not args.no_journal else None

    print(BANNER)
    print(f"Scanning {len(symbols)} symbol(s) on {timeframe.name}, "
          f"min confluence {config.strategy.min_confluence:.0%}, "
          f"min R:R {config.risk.min_risk_reward:.0f}:1\n")

    found = 0
    for symbol in symbols:
        try:
            candles = source.fetch(symbol, timeframe, config.data.lookback_bars)
            evaluation = scan_latest(candles, symbol, config)
        except TradingBotError as exc:
            print(f"{symbol:<8} could not scan: {exc}")
            continue

        if not evaluation.has_signal:
            print(no_signal_message(symbol, timeframe.name, evaluation.confluence_fraction))
            continue

        found += 1
        signal = evaluation.signal
        instrument = get_instrument(symbol)
        print()
        print(format_signal_compact(signal, instrument) if args.compact
              else format_signal(signal, instrument))
        print()
        if journal is not None:
            journal.record(signal)

    print(f"\n{found} setup(s) found across {len(symbols)} symbol(s).")
    if found and journal is not None:
        print(f"Recorded to {journal.path}")
    if found == 0:
        print("Nothing met the rules. No setup is a position too.")
    return 0


def cmd_backtest(args, config: Config) -> int:
    """Measure the strategy on history."""
    candles = _load_candles(args, config)
    symbol = args.symbol or config.data.symbols[0]

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
    symbol = args.symbol or config.data.symbols[0]

    print(BANNER)
    print(f"Calibrating {symbol} on {len(candles)} bars "
          f"({'out-of-sample only' if args.split else 'full series'})\n")
    print(format_sweep(sweep(candles, symbol, config, split=args.split)))
    return 0


def cmd_journal(args, config: Config) -> int:
    """Show what has been advised."""
    journal = Journal(args.path or config.journal_path)
    print(journal.summary())
    return 0


def cmd_data(args, config: Config) -> int:
    """Create or inspect candle files."""
    out_dir = Path(args.out or config.data.csv_dir)
    timeframe = Timeframe.parse(args.timeframe or config.data.timeframe)

    if args.generate:
        out_dir.mkdir(parents=True, exist_ok=True)
        for symbol in (args.symbols or config.data.symbols):
            source = SyntheticSource(seed=args.seed)
            candles = source.fetch(symbol, timeframe, args.bars)
            path = out_dir / f"{symbol.upper()}_{timeframe.name}.csv"
            write_csv(path, candles)
            print(f"wrote {len(candles)} bars to {path}")
        print("\nThis is synthetic data for testing the pipeline.")
        print("It is not a market. Never quote results from it as performance.")
        return 0

    if args.inspect:
        candles = load_csv(args.inspect)
        print(f"{args.inspect}: {len(candles)} bars")
        print(f"  from {candles[0].timestamp.isoformat()}")
        print(f"  to   {candles[-1].timestamp.isoformat()}")
        lo = min(c.low for c in candles)
        hi = max(c.high for c in candles)
        print(f"  range {lo:.5f} - {hi:.5f}")
        return 0

    print("Nothing to do. Pass --generate or --inspect PATH.")
    return 1


def _load_candles(args, config: Config):
    """Resolve candles for backtest/calibrate from the flags given."""
    if args.csv:
        return load_csv(args.csv)
    symbol = args.symbol or config.data.symbols[0]
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

    scan = sub.add_parser("scan", help="show what to do right now")
    scan.add_argument("--symbols", nargs="+", help="override the configured symbols")
    scan.add_argument("--timeframe", help="override the configured timeframe")
    scan.add_argument("--source", choices=["csv", "rest", "synthetic"])
    scan.add_argument("--compact", action="store_true", help="one line per signal")
    scan.add_argument("--no-journal", action="store_true", help="do not record signals")
    scan.set_defaults(func=cmd_scan)

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
    cal.set_defaults(func=cmd_calibrate)

    jour = sub.add_parser("journal", help="show journalled signals")
    jour.add_argument("--path", help="journal file")
    jour.add_argument("--list", action="store_true", help="list entries (default)")
    jour.set_defaults(func=cmd_journal)

    data = sub.add_parser("data", help="create or inspect candle files")
    data.add_argument("--generate", action="store_true", help="write synthetic sample CSVs")
    data.add_argument("--inspect", metavar="PATH", help="summarise a CSV")
    data.add_argument("--symbols", nargs="+")
    data.add_argument("--timeframe")
    data.add_argument("--bars", type=int, default=2000)
    data.add_argument("--seed", type=int, default=42)
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
