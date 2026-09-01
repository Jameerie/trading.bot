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
from ..data.base import missing_symbols
from ..data.csv_source import CsvSource, fill_commands
from ..data.synthetic import SyntheticSource
from ..errors import TradingBotError
from ..clock import session_windows
from ..exposure import analyse as analyse_exposure
from ..forecast import (
    bars_to_time,
    build_prediction,
    measure_base_rate,
    resolve_open_predictions,
    scoreboard,
)
from ..instruments import GROUPS, REGISTRY, expand_symbols, get_instrument
from ..pairs import analyse_universe, persistence_check
from ..playbook import (
    CHECK_GUIDE,
    aftercare,
    contingencies,
    invalidation_plan,
    management_plan,
    order_ticket,
    timing_plan,
)
from ..journal import Journal
from ..ledger import (
    ORIGIN_FORWARD,
    ORIGIN_REPLAY,
    BAND_ORDER,
    GRADE_ORDER,
    breakdown,
    by_direction,
    by_grade,
    by_month,
    by_session,
    by_strategy,
    by_symbol,
    calibration,
    check_attribution,
    live_status,
    load_cases,
    replay,
    snapshot,
    summarise,
)
from ..limits import evaluate_limits
from ..metrics import compute_metrics, evaluate_gate, measure_edge
from ..models import Candle, Signal, Timeframe
from ..risk_analysis import analyse, analyse_from_metrics, format_report
from ..scanner import scan_latest
from ..sessions import session_label
from ..structure import Trend

# Chart payloads are capped so a phone on mobile data is not sent a 3000-bar
# series it will only draw 200 pixels of.
MAX_CHART_BARS = 180

# A scan of the whole registry is a legitimate request — "all" is the default in
# the shipped config — so the cap sits just above the registry size rather than
# at the twenty that suited a three-pair tool.
MAX_SCAN_SYMBOLS = 80


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
    """Parse a symbol list, accepting group names as well as pairs.

    ``majors``, ``crosses``, ``all`` and the rest expand here exactly as they do
    in the config file, so the browser and the terminal accept the same input.
    """
    raw = params.get("symbols")
    if not raw:
        return config.data.resolved_symbols
    values = raw.split(",") if isinstance(raw, str) else list(raw)
    cleaned = [str(v).strip() for v in values if str(v).strip()]
    if not cleaned:
        raise ApiError("no symbols given")
    symbols = expand_symbols(cleaned)
    if len(symbols) > MAX_SCAN_SYMBOLS:
        raise ApiError(
            f"too many symbols ({len(symbols)}); {MAX_SCAN_SYMBOLS} at a time is the limit"
        )
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


def _plan_body(lines: list[str]) -> list[str]:
    """Strip a playbook block's heading, which the UI renders itself.

    Every block from ``playbook`` opens with its own title at two spaces of
    indent and continues at four or more, because the terminal card has no other
    way to label a section. The browser puts that title in the disclosure
    summary, so leaving it in the body prints it twice.
    """
    if lines and not lines[0].startswith("    "):
        return lines[1:]
    return lines


def signal_payload(signal: Signal, config: Config | None = None, prediction=None) -> dict:
    """A signal plus the presentation fields the UI needs.

    With a config, the payload also carries the playbook — the same
    step-by-step guidance the terminal card prints. The browser is not a
    lesser client: it gets the advice, not just the prices.
    """
    data = signal.to_dict()
    instrument = get_instrument(signal.symbol)
    data["digits"] = instrument.digits
    data["pip_size"] = instrument.pip_size
    data["session"] = session_label(signal.issued_at)
    data["reward_amount"] = round(signal.risk_amount * signal.risk_reward, 2)
    data["description"] = instrument.describe()
    data["peak_sessions"] = list(instrument.peak_sessions)

    if config is not None:
        clock = config.clock
        data["issued_local"] = clock.stamp(signal.issued_at)
        data["playbook"] = {
            "order": _plan_body(order_ticket(signal, instrument, config)),
            "timing": _plan_body(timing_plan(signal, instrument, clock)),
            "invalidation": _plan_body(invalidation_plan(signal, instrument, config)),
            "management": _plan_body(management_plan(signal, config, clock)),
            "contingencies": _plan_body(contingencies(signal, instrument, config)),
            "aftercare": _plan_body(aftercare(signal, config)),
        }
    if prediction is not None:
        data["prediction"] = prediction.to_dict()
        data["prediction"]["resolve_by_local"] = config.clock.stamp(prediction.resolve_by)
        data["prediction"]["entry_deadline_local"] = config.clock.stamp(
            prediction.entry_deadline
        )
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
            "resolved_symbols": config.data.resolved_symbols,
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
        "resolved": config.data.resolved_symbols,
        "groups": {name: list(members) for name, members in GROUPS.items()},
    }


def scan(params: dict, config: Config) -> dict:
    """Evaluate the latest closed bar for each requested symbol.

    The payload carries what the terminal card carries: the prediction each
    signal amounts to, its measured base rate, the playbook for placing it, and
    — for the pairs that did not qualify — which checks failed and what would
    have to change. A phone is not a worse place to get advice from.
    """
    wanted = _symbols_from(params, config)
    timeframe = Timeframe.parse(str(params.get("timeframe") or config.data.timeframe))
    source = _source_for(config, params.get("source"))
    want_chart = str(params.get("chart", "1")).lower() not in ("0", "false", "no")
    want_rates = str(params.get("base_rates", "1")).lower() not in ("0", "false", "no")
    journal = Journal(config.journal_path)
    should_journal = str(params.get("journal", "0")).lower() in ("1", "true", "yes")
    clock = config.clock

    results = []
    signals: list[Signal] = []
    base_rates: dict = {}
    # Asked once, before any fetch: a pair with no file is a setup problem the
    # reader can fix in one command, not sixty separate failures to scroll past.
    gapped = set(missing_symbols(source, wanted, timeframe))

    for symbol in wanted:
        if symbol in gapped:
            results.append({
                "symbol": symbol,
                "status": "no_data",
                "message": f"no {timeframe.name} candles on file",
            })
            continue
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
            "group": instrument.group,
            "description": instrument.describe(),
            "last_price": candles[-1].close,
            "last_bar": candles[-1].timestamp.isoformat(),
            "last_bar_local": clock.stamp(candles[-1].timestamp),
            "session": session_label(candles[-1].timestamp),
            "confluence": evaluation.confluence_fraction,
        }
        if want_chart:
            row["candles"] = candles_payload(candles)

        if evaluation.has_signal:
            signal = evaluation.signal
            signals.append(signal)
            prediction = None
            if want_rates:
                base_rates[symbol] = measure_base_rate(candles, symbol, config)
                prediction = build_prediction(signal, config, base_rates[symbol])
            row["status"] = "signal"
            row["signal"] = signal_payload(signal, config, prediction)
            if should_journal:
                recorded = journal.record_once(
                    signal, context=snapshot(evaluation, config, prediction)
                )
                row["journalled"] = recorded is not None
        else:
            row["status"] = "no_setup"
            row["guidance"] = _no_setup_payload(evaluation, config)
        results.append(row)

    found = sum(1 for r in results if r.get("status") == "signal")
    payload = {
        "scanned_at": datetime.now(timezone.utc).isoformat(),
        "scanned_at_local": clock.stamp(datetime.now(timezone.utc)),
        "timezone": clock.zone_name,
        "timezone_abbrev": clock.abbrev(),
        "timeframe": timeframe.name,
        "min_confluence": config.strategy.min_confluence,
        "min_risk_reward": config.risk.min_risk_reward,
        "found": found,
        "scanned": sum(1 for r in results if r.get("status") in ("signal", "no_setup")),
        "requested": len(wanted),
        "sessions": [
            {"name": w.name, "local": w.label, "utc": w.utc_label}
            for w in session_windows(clock, config.strategy.sessions)
        ],
        "limits": _limits_payload(config, journal),
        "results": results,
    }
    if len(signals) > 1:
        payload["exposure"] = analyse_exposure(signals, config, base_rates).to_dict()
    if gapped:
        payload["data_gaps"] = _data_gaps_payload(
            [s for s in wanted if s in gapped], timeframe, source
        )
    return payload


def _data_gaps_payload(gaps: list[str], timeframe: Timeframe, source) -> dict:
    """The pairs with no file, and the commands that fix all of them at once.

    Structured rather than prose so the UI can render one notice instead of one
    red row per pair. The pairs are still listed individually inside it: a pair
    that quietly disappeared from the scan would be indistinguishable from a
    pair that was looked at and found nothing.
    """
    return {
        "count": len(gaps),
        "symbols": gaps,
        "timeframe": timeframe.name,
        "directory": str(getattr(source, "directory", "")),
        "commands": [
            {"command": command, "note": note}
            for command, note in fill_commands(timeframe, only_missing=True)
        ],
    }


def _no_setup_payload(evaluation, config: Config) -> dict:
    """Why this pair produced nothing, and what would change that."""
    scored = evaluation.confluence
    if scored is None or evaluation.direction is None:
        return {
            "direction": None,
            "fraction": evaluation.confluence_fraction,
            "met": [],
            "missing": [],
            "summary": (
                "No directional bias to score here, or the indicators are still "
                "warming up on the history available."
            ),
        }

    from ..strategy.trend_pullback import DEFAULT_CHECKS

    weights = {check.code: check.weight for check in DEFAULT_CHECKS}
    fraction = evaluation.confluence_fraction or 0.0
    short_by = config.strategy.min_confluence - fraction
    return {
        "direction": evaluation.direction.value,
        "fraction": fraction,
        "score": scored.score,
        "max_score": scored.max_score,
        "short_by": round(short_by, 4),
        "met": [
            {"code": r.code, "detail": r.detail, "weight": r.weight} for r in scored.reasons
        ],
        "missing": [
            {
                "code": code,
                "weight": weights.get(code, 0.0),
                "title": CHECK_GUIDE.get(code, (code, ""))[0],
                "detail": CHECK_GUIDE.get(code, ("", "no description available"))[1],
            }
            for code in sorted(
                scored.missing, key=lambda c: weights.get(c, 0.0), reverse=True
            )
        ],
        "summary": (
            f"Best case is a {evaluation.direction.value}, scoring {scored.score:.0f} of "
            f"{scored.max_score:.0f} points ({fraction:.0%}). It needs "
            f"{config.strategy.min_confluence:.0%}."
        ),
        "watchlist": fraction >= config.strategy.min_confluence - 0.12,
    }


def _limits_payload(config: Config, journal: Journal) -> dict:
    """Where the account stands against its loss limits.

    Advisory only, like everything else here: the payload says a limit is
    breached; it never withholds a signal or changes one.
    """
    status = evaluate_limits(journal.read(), config)
    return {
        "enabled": status.enabled,
        "breached": status.breached,
        "daily_loss_pct": status.daily_loss_pct,
        "daily_limit_pct": status.daily_limit_pct,
        "drawdown_pct": status.drawdown_pct,
        "drawdown_limit_pct": status.drawdown_limit_pct,
        "headroom_pct": round(status.headroom_pct(), 4),
        "closed_trades": status.closed_trades,
        "breaches": [
            {"name": b.name, "limit_pct": b.limit_pct, "actual_pct": b.actual_pct}
            for b in status.breaches
        ],
        "banner": status.banner(),
    }


def _result_payload(result, config: Config) -> dict:
    """Common shape for a backtest result and its metrics."""
    metrics = compute_metrics(result.trades, config.target.confidence)
    gate = evaluate_gate(
        metrics, config.target.win_rate, config.target.min_sample, config.target.confidence
    )
    edge = measure_edge(metrics, config.target.min_sample)
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
        "edge": {
            "verdict": edge.verdict,
            "proven": edge.proven,
            "baseline": edge.baseline,
            "risk_reward": edge.risk_reward,
            "edge": edge.edge,
            "lower_bound_edge": edge.lower_bound_edge,
            "detail": edge.detail,
        },
        "equity_curve": equity,
        "trades": [t.to_dict() for t in result.trades],
    }


def backtest(params: dict, config: Config) -> dict:
    """Run a backtest, optionally split into in-sample and out-of-sample."""
    symbol = str(params.get("symbol") or config.data.resolved_symbols[0]).upper()
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
    symbol = str(params.get("symbol") or config.data.resolved_symbols[0]).upper()
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
        symbol = str(params.get("symbol") or config.data.resolved_symbols[0]).upper()
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
        "limits": _limits_payload(config, journal),
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


def pairs(params: dict, config: Config) -> dict:
    """Win rate by pair across the requested universe.

    The expensive endpoint: it walks every bar of history for every symbol. The
    bar count is capped rather than left to the caller, because a phone asking
    for sixty pairs at twenty thousand bars is a request nobody wants answered.
    """
    wanted = _symbols_from(params, config)
    timeframe = Timeframe.parse(str(params.get("timeframe") or config.data.timeframe))
    source = _source_for(config, params.get("source"))
    bars = _as_int(params, "bars", max(config.data.lookback_bars, 1500), 250, 8000)
    split = _as_float(params, "split", 0.7, 0.0, 0.9)

    gapped = set(missing_symbols(source, wanted, timeframe))
    candles_by_symbol: dict[str, list[Candle]] = {}
    for symbol in wanted:
        if symbol in gapped:
            continue
        try:
            candles_by_symbol[symbol.upper()] = source.fetch(symbol, timeframe, bars)
        except TradingBotError:
            continue

    report = analyse_universe(
        candles_by_symbol, config, split=split or None, symbols=wanted
    )
    payload = report.to_dict()
    payload["bars_requested"] = bars
    if gapped:
        payload["data_gaps"] = _data_gaps_payload(
            [s for s in wanted if s in gapped], timeframe, source
        )

    if str(params.get("persistence", "0")).lower() in ("1", "true", "yes"):
        check = persistence_check(candles_by_symbol, config)
        payload["persistence"] = {
            "verdict": check.verdict(),
            "selected": list(check.selected),
            "rejected": list(check.rejected),
            "gain_r": round(check.gain_r, 4),
            "sign_agreement": round(check.sign_agreement, 4),
            "pairs_compared": check.pairs_compared,
            "everything": {
                "trades": check.everything.trades,
                "win_rate": round(check.everything.win_rate, 4),
                "expectancy_r": round(check.everything.expectancy_r, 4),
            },
            "chosen": {
                "trades": check.chosen.trades,
                "win_rate": round(check.chosen.win_rate, 4),
                "expectancy_r": round(check.chosen.expectancy_r, 4),
            },
        }
    return payload


def forecast(params: dict, config: Config) -> dict:
    """Live predictions and the forward scoreboard.

    Separate from ``/api/backtest`` on purpose, and the payload says so: a
    backtest replays trades whose outcome was already known, and cannot put a
    single entry on this board.
    """
    journal = Journal(config.journal_path)
    clock = config.clock
    now = datetime.now(timezone.utc)

    live = []
    for entry in journal.open_entries():
        data = entry.signal
        issued = datetime.fromisoformat(entry.issued_at)
        timeframe = Timeframe.parse(str(data.get("timeframe", config.data.timeframe)))
        deadline = bars_to_time(issued, config.backtest.max_bars_in_trade, timeframe)
        live.append({
            "id": entry.entry_id,
            "symbol": data.get("symbol"),
            "direction": data.get("direction"),
            "entry": data.get("entry"),
            "stop_loss": data.get("stop_loss"),
            "take_profit": data.get("take_profit"),
            "grade": data.get("grade"),
            "risk_reward": data.get("risk_reward"),
            "made_at": entry.issued_at,
            "made_at_local": clock.stamp(issued),
            "resolve_by": deadline.isoformat(),
            "resolve_by_local": clock.stamp(deadline),
            "overdue": now > deadline,
        })

    board = scoreboard(journal, config.target.confidence)
    return {
        "timezone": clock.zone_name,
        "live": live,
        "scoreboard": {
            "made": board.made,
            "resolved": board.resolved,
            "still_open": board.still_open,
            "win_rate": round(board.metrics.win_rate, 4),
            "interval_low": round(board.metrics.win_rate_interval.low, 4),
            "interval_high": round(board.metrics.win_rate_interval.high, 4),
            "expectancy_r": round(board.metrics.expectancy_r, 4),
            "total_r": round(board.metrics.total_r, 3),
            "has_verdict": board.has_verdict,
            "lines": board.summary(clock),
        },
        "note": (
            "This board counts only predictions written down before the outcome was "
            "knowable. A backtest cannot add to it."
        ),
    }


def forecast_resolve(params: dict, config: Config) -> dict:
    """Settle open predictions against fresh candles, by the backtest's own rules."""
    journal = Journal(config.journal_path)
    source = _source_for(config, params.get("source"))
    reports = resolve_open_predictions(journal, source, config)
    return {
        "checked": len(reports),
        "resolved": sum(1 for r in reports if r["status"] == "resolved"),
        "results": reports,
    }


def _ledger_payload(cases: list, config: Config, origin: str, limit: int) -> dict:
    """The shape both ledgers share: the record, the scorecards, the case files."""
    clock = config.clock
    confidence = config.target.confidence
    resolved = [c for c in cases if c.is_resolved]
    months = sorted({by_month(c) for c in resolved})
    return {
        "origin": origin,
        "timezone": clock.zone_name,
        "timezone_abbrev": clock.abbrev(),
        "count": len(cases),
        "shown": min(len(cases), limit),
        "summary": summarise(cases, config, origin).to_dict(clock),
        "scorecards": {
            "calibration": [b.to_dict() for b in calibration(resolved, confidence)],
            "grade": [b.to_dict() for b in breakdown(resolved, by_grade, confidence, GRADE_ORDER)],
            "symbol": [b.to_dict() for b in breakdown(resolved, by_symbol, confidence)],
            "direction": [
                b.to_dict() for b in breakdown(resolved, by_direction, confidence, ["buy", "sell"])
            ],
            "session": [b.to_dict() for b in breakdown(resolved, by_session, confidence)],
            "month": [b.to_dict() for b in breakdown(resolved, by_month, confidence, months)],
            "strategy": [b.to_dict() for b in breakdown(resolved, by_strategy, confidence)],
            "checks": [e.to_dict() for e in check_attribution(resolved, confidence)],
            "bands": BAND_ORDER,
        },
        # Newest first: the call you are waiting on is the one you want at the top.
        "cases": [c.to_dict(clock) for c in reversed(cases[-limit:])],
        "note": (
            "Replayed from history: every outcome was in the file before the call was "
            "made. This is a backtest wearing a diary, not a track record."
            if origin == ORIGIN_REPLAY else
            "Every entry here was written down before its outcome existed, then settled "
            "by the same rule the backtest uses. A backtest cannot add to it."
        ),
    }


def ledger(params: dict, config: Config) -> dict:
    """The forward ledger, with every open prediction judged against fresh candles.

    ``live=0`` skips the candle fetches for a caller that only wants the record;
    the open predictions are then listed with their case files but no status.
    """
    journal = Journal(config.journal_path)
    clock = config.clock
    limit = _as_int(params, "limit", 100, 1, 1000)
    cases = load_cases(journal, config)
    payload = _ledger_payload(cases, config, ORIGIN_FORWARD, limit)
    payload["path"] = str(journal.path)

    want_live = str(params.get("live", "1")).lower() not in ("0", "false", "no")
    live = []
    open_cases = [c for c in cases if c.is_open]
    if want_live and open_cases:
        source = _source_for(config, params.get("source"))
        cache: dict = {}
        for case in open_cases:
            key = (case.symbol, case.signal.timeframe)
            if key not in cache:
                try:
                    cache[key] = source.fetch(
                        case.symbol, case.signal.timeframe, config.data.lookback_bars
                    )
                except TradingBotError as exc:
                    cache[key] = exc
            candles = cache[key]
            if isinstance(candles, Exception):
                live.append({
                    "id": case.id,
                    "symbol": case.symbol,
                    "direction": case.direction.value,
                    "state": "NO DATA",
                    "advice": [f"could not judge it: {candles}"],
                    "detail": str(candles),
                })
                continue
            live.append(live_status(case, candles, config).to_dict(clock))
    payload["live"] = live
    return payload


def ledger_replay(params: dict, config: Config) -> dict:
    """What the model would have said at every bar of a history, and what followed."""
    symbol = str(params.get("symbol") or config.data.resolved_symbols[0]).upper()
    timeframe = Timeframe.parse(str(params.get("timeframe") or config.data.timeframe))
    bars = _as_int(params, "bars", config.data.lookback_bars, 250, 20_000)
    limit = _as_int(params, "limit", 100, 1, 1000)
    source = _source_for(config, params.get("source"))
    candles = source.fetch(symbol, timeframe, bars)

    raw_split = params.get("split", 0)
    fraction = None if raw_split in (None, "", 0, "0") else _as_float(
        params, "split", 0.7, 0.1, 0.9
    )
    start = int(len(candles) * fraction) if fraction else None
    cases = replay(candles, symbol, config, start=start)
    payload = _ledger_payload(cases, config, ORIGIN_REPLAY, limit)
    payload.update({
        "symbol": symbol,
        "timeframe": timeframe.name,
        "bars": len(candles) - (start or 0),
        "split": fraction,
        "first_bar": candles[start or 0].timestamp.isoformat(),
        "last_bar": candles[-1].timestamp.isoformat(),
    })
    return payload


# Route table. Each entry is (handler, methods).
ROUTES: dict[str, tuple] = {
    "/api/health": (health, ("GET",)),
    "/api/settings": (settings, ("GET",)),
    "/api/symbols": (symbols, ("GET",)),
    "/api/scan": (scan, ("GET", "POST")),
    "/api/pairs": (pairs, ("GET", "POST")),
    "/api/forecast": (forecast, ("GET",)),
    "/api/forecast/resolve": (forecast_resolve, ("POST",)),
    "/api/ledger": (ledger, ("GET",)),
    "/api/ledger/replay": (ledger_replay, ("GET", "POST")),
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
