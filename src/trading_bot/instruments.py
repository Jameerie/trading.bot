"""Per-instrument conventions.

Pip size is the reason this module exists. A pip is 0.0001 on most pairs but
0.01 on JPY-quoted pairs, and getting that wrong silently scales every stop,
target and position size by 100. Nothing outside this module should ever write
a bare ``0.0001``.
"""

from __future__ import annotations

from dataclasses import dataclass

from .errors import ConfigError

# Standard lot = 100_000 units of the base currency.
STANDARD_LOT = 100_000.0


@dataclass(frozen=True)
class Instrument:
    """Static conventions for one tradable symbol."""

    symbol: str
    pip_size: float
    digits: int
    typical_spread_pips: float
    contract_size: float = STANDARD_LOT

    @property
    def base(self) -> str:
        return self.symbol[:3]

    @property
    def quote(self) -> str:
        return self.symbol[3:6]


def _fx(symbol: str, spread_pips: float) -> Instrument:
    """Build a standard FX instrument, taking pip size from the quote currency."""
    jpy = symbol.upper().endswith("JPY")
    return Instrument(
        symbol=symbol.upper(),
        pip_size=0.01 if jpy else 0.0001,
        digits=3 if jpy else 5,
        typical_spread_pips=spread_pips,
    )


# Typical spreads are retail-broker averages during London/NY overlap. They are
# deliberately pessimistic: a backtest that assumes a tighter spread than the
# user will actually pay reports a win rate the user cannot reproduce.
REGISTRY: dict[str, Instrument] = {
    inst.symbol: inst
    for inst in (
        _fx("EURUSD", 0.8),
        _fx("GBPUSD", 1.2),
        _fx("USDJPY", 0.9),
        _fx("USDCHF", 1.3),
        _fx("AUDUSD", 1.0),
        _fx("NZDUSD", 1.6),
        _fx("USDCAD", 1.4),
        _fx("EURGBP", 1.1),
        _fx("EURJPY", 1.5),
        _fx("GBPJPY", 2.2),
        _fx("AUDJPY", 1.8),
        _fx("EURAUD", 2.0),
        _fx("GBPAUD", 2.8),
        _fx("XAUUSD", 25.0),
    )
}


def get_instrument(symbol: str) -> Instrument:
    """Look up an instrument, falling back to a sane 5-digit FX default.

    Unknown symbols are not an error — the user may trade an exotic we have not
    catalogued — but they get a conservative wide spread so results are not
    flattering by accident.
    """
    key = symbol.upper().replace("/", "").replace("_", "")
    if key in REGISTRY:
        return REGISTRY[key]
    if len(key) < 6:
        raise ConfigError(f"unrecognised symbol {symbol!r}: expected a 6-letter pair")
    return _fx(key, spread_pips=3.0)


def pips_between(instrument: Instrument, price_a: float, price_b: float) -> float:
    """Absolute distance between two prices, expressed in pips."""
    return abs(price_a - price_b) / instrument.pip_size


def price_from_pips(instrument: Instrument, pips: float) -> float:
    """Convert a pip distance into a price offset."""
    return pips * instrument.pip_size


def round_price(instrument: Instrument, price: float) -> float:
    """Round to the instrument's quoted precision."""
    return round(price, instrument.digits)


def same_price(instrument: Instrument, a: float, b: float, tol_pips: float = 0.05) -> bool:
    """Compare prices with a pip tolerance instead of exact float equality."""
    return pips_between(instrument, a, b) <= tol_pips
