"""JSON API handlers.

Deliberately separated from the HTTP plumbing in ``server.py``: every handler
here is a plain function from a parameter dict to a JSON-serialisable dict, so
the whole API is testable without opening a socket.

Handlers never raise for ordinary user error. They return ``{"error": ...}`` with
a status code, because a stack trace is not a useful thing to render on a phone.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone

from .. import __version__
from ..backtest import run_backtest, split_backtest
from ..calibrate import sweep
from ..config import Config
from ..data.csv_source import CsvSource
from ..data.synthetic import SyntheticSource
from ..errors import TradingBotError
from ..instruments import REGISTRY, get_instrument
from ..journal import Journal
from ..metrics import compute_metrics, evaluate_gate
from ..models import Candle, Signal, Timeframe
from ..risk_analysis import analyse, analyse_from_metrics, format_report
from ..scanner import scan_latest
from ..sessions import session_label
from ..structure import Trend

# Chart payloads are capped so a phone on mobile data is not sent a 3000-bar
# series it will only draw 200 pixels of.
MAX_CHART_BARS = 180


class ApiError(Exception):
    """A client-visible failure with an HTTP status."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


def _source_for(config: Config, name: str | None):
    """Resolve a data source by name, defaulting to the configured one."""
    kind = (name or config.data.source).lower()
    if kind == "csv":
        return CsvSource(config.data.csv_dir)
    if kind == "synthetic":
        return SyntheticSource()
    if kind == "rest":
        from ..data.rest_source import build_rest_source

        return build_rest_source(config.data.provider, config.data.api_key)
    raise ApiError(f"unknown data source {kind!r}; expected csv, synthetic or rest")


def _as_int(params: dict, key: str, default: int, low: int, high: int) -> int:
    """Read a bounded integer parameter, rejecting nonsense rather than clamping."""
    raw = params.get(key, default)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise ApiError(f"{key} must be a whole number, got {raw!r}") from None
    if not low <= value <= high:
        raise ApiError(f"{key} must be between {low} and {high}, got {value}")
    return value


def _as_float(params: dict, key: str, default: float, low: float, high: float) -> float:
    raw = params.get(key, default)
    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise ApiError(f"{key} must be a number, got {raw!r}") from None
    if not low <= value <= high:
        raise ApiError(f"{key} must be between {low} and {high}, got {value}")
    return value


def _symbols_from(params: dict, config: Config) -> list[str]:
    """Parse a symbol list from a comma-separated string or a JSON array."""
    raw = params.get("symbols")
    if not raw:
        return list(config.data.symbols)
    values = raw.split(",") if isinstance(raw, str) else list(raw)
    symbols = [str(v).strip().upper() for v in values if str(v).strip()]
    if not symbols:
        raise ApiError("no symbols given")
    if len(symbols) > 20:
        raise ApiError(f"too many symbols ({len(symbols)}); 20 at a time is the limit")
    return symbols


def candles_payload(candles: list[Candle], limit: int = MAX_CHART_BARS) -> list[dict]:
    """Trim and flatten candles for the chart."""
    return [
        {
            "t": c.timestamp.isoformat(),
            "o": c.open,
            "h": c.high,
            "l": c.low,
            "c": c.close,
        }
        for c in candles[-limit:]
    ]


def signal_payload(signal: Signal) -> dict:
    """A signal plus the presentation fields the UI needs."""
    data = signal.to_dict()
    instrument = get_instrument(signal.symbol)
    data["digits"] = instrument.digits
    data["pip_size"] = instrument.pip_size
    data["session"] = session_label(signal.issued_at)
    data["reward_amount"] = round(signal.risk_amount * signal.risk_reward, 2)
    return data


# --------------------------------------------------------------------- handlers


def health(params: dict, config: Config) -> dict:
    """Liveness plus the settings that shape every other answer."""
    return {
        "status": "ok",
        "version": __version__,
        "utc": datetime.now(timezone.utc).isoformat(),
        "min_risk_reward": config.risk.min_risk_reward,
        "min_confluence": config.strategy.min_confluence,
        "strategy": config.strategy.name,
        "executes_trades": False,
    }


def settings(params: dict, config: Config) -> dict:
    """Configuration as the UI needs it.

    No credentials are included. ``api_key`` is read from the environment and is
    never part of this payload, only whether one is present.
    """
    return {
        "account": {
            "balance": config.account.balance,
            "currency": config.account.currency,
            "risk_per_trade_pct": config.account.risk_per_trade_pct,
        },
        "risk": {
            "min_risk_reward": config.risk.min_risk_reward,
            "max_risk_reward": config.risk.max_risk_reward,
            "min_stop_pips": config.risk.min_stop_pips,
            "max_stop_pips": config.risk.max_stop_pips,
        },
        "strategy": {
            "name": config.strategy.name,
            "min_confluence": config.strategy.min_confluence,
            "sessions": list(config.strategy.sessions),
        },
        "data": {
            "source": config.data.source,
            "symbols": list(config.data.symbols),
            "timeframe": config.data.timeframe,
            "htf_timeframe": config.data.htf_timeframe,
            "has_api_key": config.data.api_key is not None,
        },
        "target": {
            "win_rate": config.target.win_rate,
            "min_sample": config.target.min_sample,
        },
    }


def symbols(params: dict, config: Config) -> dict:
    """Instruments the tool knows about."""
    return {
        "symbols": [
            {
                "symbol": inst.symbol,
                "pip_size": inst.pip_size,
                "digits": inst.digits,
                "typical_spread_pips": inst.typical_spread_pips,
            }
            for inst in sorted(REGISTRY.values(), key=lambda i: i.symbol)
        ],
        "timeframes": [tf.name for tf in Timeframe],
        "configured": list(config.data.symbols),
    }


def scan(params: dict, config: Config) -> dict:
    """Evaluate the latest closed bar for each requested symbol."""
    wanted = _symbols_from(params, config)
    timeframe = Timeframe.parse(str(params.get("timeframe") or config.data.timeframe))
    source = _source_for(config, params.get("source"))
    want_chart = str(params.get("chart", "1")).lower() not in ("0", "false", "no")
    journal = Journal(config.journal_path)
    should_journal = str(params.get("journal", "0")).lower() in ("1", "true", "yes")

    results = []
    for symbol in wanted:
        try:
            candles = source.fetch(symbol, timeframe, config.data.lookback_bars)
            evaluation = scan_latest(candles, symbol, config)
        except TradingBotError as exc:
            results.append({"symbol": symbol, "status": "error", "message": str(exc)})
            continue

        instrument = get_instrument(symbol)
        row = {
            "symbol": symbol,
            "timeframe": timeframe.name,
            "digits": instrument.digits,
            "last_price": candles[-1].close,
            "last_bar": candles[-1].timestamp.isoformat(),
            "session": session_label(candles[-1].timestamp),
            "confluence": evaluation.confluence_fraction,
        }
        if want_chart:
            row["candles"] = candles_payload(candles)

        if evaluation.has_signal:
            row["status"] = "signal"
            row["signal"] = signal_payload(evaluation.signal)
            if should_journal:
                recorded = journal.record_once(evaluation.signal)
                row["journalled"] = recorded is not None
        else:
            row["status"] = "no_setup"
        results.append(row)

    found = sum(1 for r in results if r.get("status") == "signal")
    return {
        "scanned_at": datetime.now(timezone.utc).isoformat(),
        "timeframe": timeframe.name,
        "min_confluence": config.strategy.min_confluence,
        "min_risk_reward": config.risk.min_risk_reward,
        "found": found,
        "results": results,
    }


def _result_payload(result, config: Config) -> dict:
    """Common shape for a backtest result and its metrics."""
    metrics = compute_metrics(result.trades, config.target.confidence)
    gate = evaluate_gate(
        metrics, config.target.win_rate, config.target.min_sample, config.target.confidence
    )
    interval = metrics.win_rate_interval
    equity, running = [], 0.0
    for trade in result.trades:
        running += trade.r_multiple
        equity.append(round(running, 4))

    return {
        "label": result.label,
        "symbol": result.symbol,
        "bars_tested": result.bars_tested,
        "first_bar": result.first_bar,
        "last_bar": result.last_bar,
        "metrics": {
            "trades": metrics.trades,
            "wins": metrics.wins,
            "losses": metrics.losses,
            "expired": metrics.expired,
            "win_rate": metrics.win_rate,
            "interval_low": interval.low,
            "interval_high": interval.high,
            "expectancy_r": metrics.expectancy_r,
            "total_r": metrics.total_r,
            "profit_factor": metrics.profit_factor if metrics.losses else None,
            "max_drawdown_r": metrics.max_drawdown_r,
            "max_win_streak": metrics.max_win_streak,
            "max_loss_streak": metrics.max_loss_streak,
            "average_win_r": metrics.average_win_r,
            "average_loss_r": metrics.average_loss_r,
            "average_bars_held": metrics.average_bars_held,
        },
        "gate": {"verdict": gate.verdict, "passed": gate.passed, "detail": gate.detail},
        "equity_curve": equity,
        "trades": [t.to_dict() for t in result.trades],
    }


def backtest(params: dict, config: Config) -> dict:
    """Run a backtest, optionally split into in-sample and out-of-sample."""
    symbol = str(params.get("symbol") or config.data.symbols[0]).upper()
    timeframe = Timeframe.parse(str(params.get("timeframe") or config.data.timeframe))
    bars = _as_int(params, "bars", config.data.lookback_bars, 250, 20_000)
    source = _source_for(config, params.get("source"))
    candles = source.fetch(symbol, timeframe, bars)

    split = params.get("split")
    if split not in (None, "", 0, "0"):
        fraction = _as_float(params, "split", 0.7, 0.1, 0.9)
        in_sample, out_sample = split_backtest(candles, symbol, config, fraction)
        return {
            "symbol": symbol,
            "bars": len(candles),
            "split": fraction,
            "in_sample": _result_payload(in_sample, config),
            "out_of_sample": _result_payload(out_sample, config),
        }

    return {
        "symbol": symbol,
        "bars": len(candles),
        "split": None,
        "result": _result_payload(run_backtest(candles, symbol, config), config),
    }


def calibrate(params: dict, config: Config) -> dict:
    """Sweep the confluence threshold."""
    symbol = str(params.get("symbol") or config.data.symbols[0]).upper()
    timeframe = Timeframe.parse(str(params.get("timeframe") or config.data.timeframe))
    bars = _as_int(params, "bars", config.data.lookback_bars, 250, 20_000)
    source = _source_for(config, params.get("source"))
    candles = source.fetch(symbol, timeframe, bars)

    raw_split = params.get("split", 0.7)
    fraction = None if raw_split in (None, "", 0, "0") else _as_float(
        params, "split", 0.7, 0.1, 0.9
    )
    result = sweep(candles, symbol, config, split=fraction)

    return {
        "symbol": result.symbol,
        "bars": len(candles),
        "out_of_sample_only": fraction is not None,
        "recommended": result.recommended,
        "rationale": result.rationale,
        "rows": [
            {
                "threshold": row.threshold,
                "trades": row.metrics.trades,
                "win_rate": row.metrics.win_rate,
                "interval_low": row.metrics.win_rate_interval.low,
                "interval_high": row.metrics.win_rate_interval.high,
                "expectancy_r": row.metrics.expectancy_r,
                "total_r": row.metrics.total_r,
                "verdict": row.verdict,
            }
            for row in result.rows
        ],
    }


def risk(params: dict, config: Config) -> dict:
    """Position sizing, either from an explicit win rate or from a backtest."""
    reward = _as_float(params, "reward", config.risk.min_risk_reward, 1.0, 20.0)
    trades = _as_int(params, "trades", 60, 5, 2000)
    trials = _as_int(params, "trials", 3000, 200, 20_000)

    if params.get("from_backtest"):
        symbol = str(params.get("symbol") or config.data.symbols[0]).upper()
        timeframe = Timeframe.parse(str(params.get("timeframe") or config.data.timeframe))
        bars = _as_int(params, "bars", config.data.lookback_bars, 250, 20_000)
        source = _source_for(config, params.get("source"))
        candles = source.fetch(symbol, timeframe, bars)
        result = run_backtest(candles, symbol, config)
        metrics = compute_metrics(result.trades, config.target.confidence)
        if metrics.is_empty:
            raise ApiError(
                f"the backtest on {symbol} produced no trades, so there is nothing to size from"
            )
        report = analyse_from_metrics(metrics, reward, trades, trials, config.target.confidence)
    else:
        win_rate = _as_float(params, "win_rate", 0.30, 0.01, 0.99)
        report = analyse(win_rate, reward, trades, trials, source="assumed")

    balance = config.account.balance
    return {
        "win_rate": report.win_rate,
        "source": report.win_rate_source,
        "reward": report.reward,
        "trades_per_period": report.trades_per_period,
        "breakeven": report.breakeven,
        "expectancy": report.expectancy,
        "kelly": report.kelly,
        "recommended_risk": report.recommended_risk,
        "recommended_amount": round(balance * report.recommended_risk, 2),
        "balance": balance,
        "currency": config.account.currency,
        "profitable": report.is_profitable,
        "caution": report.caution,
        "rows": [
            {
                "risk_fraction": row.risk_fraction,
                "median_multiple": row.median_multiple,
                "p05_multiple": row.p05_multiple,
                "p95_multiple": row.p95_multiple,
                "median_drawdown": row.median_drawdown,
                "p95_drawdown": row.p95_drawdown,
                "prob_lose_half": row.prob_lose_half,
                "label": row.label,
            }
            for row in report.rows
        ],
        "text": format_report(report, balance, config.account.currency),
    }


def journal_list(params: dict, config: Config) -> dict:
    """Everything advised, and what it did."""
    journal = Journal(config.journal_path)
    entries = journal.read()
    metrics = journal.live_metrics(config.target.confidence)
    interval = metrics.win_rate_interval

    return {
        "path": str(journal.path),
        "count": len(entries),
        "open": sum(1 for e in entries if e.is_open),
        "closed": sum(1 for e in entries if not e.is_open),
        "entries": [
            {
                "id": entry.entry_id,
                "recorded_at": entry.recorded_at.isoformat(),
                "issued_at": entry.issued_at,
                "symbol": entry.symbol,
                "direction": entry.direction,
                "grade": entry.signal.get("grade"),
                "entry": entry.signal.get("entry"),
                "stop_loss": entry.signal.get("stop_loss"),
                "take_profit": entry.signal.get("take_profit"),
                "risk_reward": entry.signal.get("risk_reward"),
                "digits": entry.signal.get("digits", 5),
                "outcome": entry.outcome,
                "exit_price": entry.exit_price,
                "r_multiple": entry.r_multiple,
                "closed_at": entry.closed_at.isoformat() if entry.closed_at else None,
                "is_open": entry.is_open,
                "note": entry.note,
            }
            for entry in reversed(entries[-200:])
        ],
        "live": {
            "trades": metrics.trades,
            "win_rate": metrics.win_rate,
            "interval_low": interval.low,
            "interval_high": interval.high,
            "expectancy_r": metrics.expectancy_r,
            "total_r": metrics.total_r,
        },
    }


def journal_close(params: dict, config: Config) -> dict:
    """Record how a journalled signal actually finished."""
    entry_id = str(params.get("id") or "").strip()
    if not entry_id:
        raise ApiError("id is required")
    if "exit_price" not in params:
        raise ApiError("exit_price is required")
    exit_price = _as_float(params, "exit_price", 0.0, 1e-9, 1e9)

    journal = Journal(config.journal_path)
    try:
        entry = journal.close(entry_id, exit_price, note=str(params.get("note") or ""))
    except TradingBotError as exc:
        raise ApiError(str(exc), status=404 if "no journalled signal" in str(exc) else 409)

    return {
        "id": entry.entry_id,
        "outcome": entry.outcome,
        "r_multiple": entry.r_multiple,
        "exit_price": entry.exit_price,
        "closed_at": entry.closed_at.isoformat() if entry.closed_at else None,
    }


# Route table. Each entry is (handler, methods).
ROUTES: dict[str, tuple] = {
    "/api/health": (health, ("GET",)),
    "/api/settings": (settings, ("GET",)),
    "/api/symbols": (symbols, ("GET",)),
    "/api/scan": (scan, ("GET", "POST")),
    "/api/backtest": (backtest, ("GET", "POST")),
    "/api/calibrate": (calibrate, ("GET", "POST")),
    "/api/risk": (risk, ("GET", "POST")),
    "/api/journal": (journal_list, ("GET",)),
    "/api/journal/close": (journal_close, ("POST",)),
}


def dispatch(path: str, method: str, params: dict, config: Config) -> tuple[int, dict]:
    """Route a request to a handler, converting domain errors into responses."""
    route = ROUTES.get(path)
    if route is None:
        return 404, {"error": f"no such endpoint: {path}"}
    handler, methods = route
    if method not in methods:
        return 405, {"error": f"{method} not allowed on {path}; use {' or '.join(methods)}"}
    try:
        return 200, handler(params, config)
    except ApiError as exc:
        return exc.status, {"error": exc.message}
    except TradingBotError as exc:
        return 400, {"error": str(exc)}
    except (ValueError, KeyError, IndexError) as exc:
        return 400, {"error": f"{type(exc).__name__}: {exc}"}
