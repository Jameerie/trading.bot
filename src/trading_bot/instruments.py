"""Per-instrument conventions.

Pip size is the reason this module exists. A pip is 0.0001 on most pairs but
0.01 on JPY-quoted pairs, and getting that wrong silently scales every stop,
target and position size by 100. Nothing outside this module should ever write
a bare ``0.0001``.

The registry covers the whole tradable universe rather than a handful of pairs,
because a setup you never looked at is a setup you never had. Every symbol also
carries the sessions in which it is actually liquid, so the advice can tell a
human *when* to be at the screen for that pair rather than only what to do.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .errors import ConfigError

# Standard lot = 100_000 units of the base currency. Metals differ; see below.
STANDARD_LOT = 100_000.0

# Quote currencies priced to two decimals rather than four. A pip on these is
# 0.01, exactly as on JPY pairs, and treating them as 0.0001 would size every
# position a hundred times too large.
BIG_PIP_QUOTES = frozenset({"JPY", "HUF"})

# Which session each currency is most liquid in. A pair's liquid window is the
# union of its two legs': AUDJPY trades in Tokyo, EURUSD across London and New
# York, GBPJPY in both. This is what makes "be at the screen at 09:00" possible.
CURRENCY_SESSIONS: dict[str, tuple[str, ...]] = {
    "USD": ("newyork",),
    "CAD": ("newyork",),
    "MXN": ("newyork",),
    "BRL": ("newyork",),
    "EUR": ("london",),
    "GBP": ("london",),
    "CHF": ("london",),
    "SEK": ("london",),
    "NOK": ("london",),
    "DKK": ("london",),
    "PLN": ("london",),
    "CZK": ("london",),
    "HUF": ("london",),
    "TRY": ("london",),
    "ZAR": ("london",),
    "ILS": ("london",),
    "JPY": ("tokyo",),
    "AUD": ("sydney", "tokyo"),
    "NZD": ("sydney", "tokyo"),
    "SGD": ("tokyo",),
    "HKD": ("tokyo",),
    "CNH": ("tokyo",),
    "THB": ("tokyo",),
    "INR": ("tokyo",),
    # Metals trade around the clock but their volume is London and COMEX.
    "XAU": ("london", "newyork"),
    "XAG": ("london", "newyork"),
    "XPT": ("london", "newyork"),
    "XPD": ("london", "newyork"),
}

CURRENCY_NAMES: dict[str, str] = {
    "USD": "US dollar", "EUR": "euro", "GBP": "British pound", "JPY": "Japanese yen",
    "CHF": "Swiss franc", "AUD": "Australian dollar", "NZD": "New Zealand dollar",
    "CAD": "Canadian dollar", "SEK": "Swedish krona", "NOK": "Norwegian krone",
    "DKK": "Danish krone", "PLN": "Polish zloty", "CZK": "Czech koruna",
    "HUF": "Hungarian forint", "TRY": "Turkish lira", "ZAR": "South African rand",
    "MXN": "Mexican peso", "SGD": "Singapore dollar", "HKD": "Hong Kong dollar",
    "CNH": "offshore Chinese yuan", "THB": "Thai baht", "ILS": "Israeli shekel",
    "INR": "Indian rupee", "BRL": "Brazilian real",
    "XAU": "gold", "XAG": "silver", "XPT": "platinum", "XPD": "palladium",
}


@dataclass(frozen=True)
class Instrument:
    """Static conventions for one tradable symbol."""

    symbol: str
    pip_size: float
    digits: int
    typical_spread_pips: float
    contract_size: float = STANDARD_LOT
    group: str = "majors"
    peak_sessions: tuple[str, ...] = field(default=("london", "newyork"))
    # How many pips of this instrument equal one pip of a major, in relative-price
    # terms. See STOP_SCALE_NOTE below for why a bare pip bound does not travel.
    stop_scale: float = 1.0

    @property
    def base(self) -> str:
        return self.symbol[:3]

    @property
    def quote(self) -> str:
        return self.symbol[3:6]

    @property
    def currencies(self) -> tuple[str, str]:
        return self.base, self.quote

    @property
    def is_metal(self) -> bool:
        return self.group == "metals"

    def describe(self) -> str:
        """Plain-English name of the pair, for people who do not think in tickers."""
        base = CURRENCY_NAMES.get(self.base, self.base)
        quote = CURRENCY_NAMES.get(self.quote, self.quote)
        if self.is_metal:
            return f"{base} priced in {quote}"
        return f"{base} against {quote}"


def _sessions_for(symbol: str) -> tuple[str, ...]:
    """Union of the two legs' liquid sessions, in a stable order."""
    order = ("sydney", "tokyo", "london", "newyork")
    legs = set(CURRENCY_SESSIONS.get(symbol[:3], ())) | set(CURRENCY_SESSIONS.get(symbol[3:6], ()))
    found = tuple(name for name in order if name in legs)
    return found or ("london", "newyork")


def _fx(
    symbol: str, spread_pips: float, group: str = "majors", stop_scale: float = 1.0
) -> Instrument:
    """Build a standard FX instrument, taking pip size from the quote currency."""
    symbol = symbol.upper()
    big_pip = symbol[3:6] in BIG_PIP_QUOTES
    return Instrument(
        symbol=symbol,
        pip_size=0.01 if big_pip else 0.0001,
        digits=3 if big_pip else 5,
        typical_spread_pips=spread_pips,
        group=group,
        peak_sessions=_sessions_for(symbol),
        stop_scale=stop_scale,
    )


def _metal(
    symbol: str,
    pip_size: float,
    digits: int,
    spread_pips: float,
    contract_size: float,
    stop_scale: float,
) -> Instrument:
    """Metals quote and size differently from FX: gold is 100 oz a lot, not 100_000."""
    symbol = symbol.upper()
    return Instrument(
        symbol=symbol,
        pip_size=pip_size,
        digits=digits,
        typical_spread_pips=spread_pips,
        contract_size=contract_size,
        group="metals",
        peak_sessions=_sessions_for(symbol),
        stop_scale=stop_scale,
    )


# The seven USD pairs. Tightest spreads, deepest books, and the only pairs where
# a 1:4 target is regularly reachable inside a working day.
_MAJORS = (
    _fx("EURUSD", 0.8), _fx("GBPUSD", 1.2), _fx("USDJPY", 0.9), _fx("USDCHF", 1.3),
    _fx("AUDUSD", 1.0), _fx("NZDUSD", 1.6), _fx("USDCAD", 1.4),
)

# Every cross between the eight major currencies: 28 pairs in total once the
# USD majors above are counted. Crosses trend more cleanly than majors and cost
# more to hold, which is exactly the trade-off the R:R floor is there to judge.
_CROSSES = (
    _fx("EURGBP", 1.1, "crosses"), _fx("EURJPY", 1.5, "crosses"),
    _fx("EURCHF", 1.5, "crosses"), _fx("EURAUD", 2.0, "crosses"),
    _fx("EURNZD", 3.0, "crosses"), _fx("EURCAD", 2.2, "crosses"),
    _fx("GBPJPY", 2.2, "crosses"), _fx("GBPCHF", 2.6, "crosses"),
    _fx("GBPAUD", 2.8, "crosses"), _fx("GBPNZD", 4.0, "crosses"),
    _fx("GBPCAD", 3.0, "crosses"),
    _fx("AUDJPY", 1.8, "crosses"), _fx("AUDNZD", 2.2, "crosses"),
    _fx("AUDCAD", 2.0, "crosses"), _fx("AUDCHF", 2.2, "crosses"),
    _fx("NZDJPY", 2.2, "crosses"), _fx("NZDCAD", 2.8, "crosses"),
    _fx("NZDCHF", 2.8, "crosses"),
    _fx("CADJPY", 2.0, "crosses"), _fx("CADCHF", 2.4, "crosses"),
    _fx("CHFJPY", 2.4, "crosses"),
)

# Emerging-market and Scandinavian pairs. Spreads here are wide enough that the
# cost model, not the setup, usually decides whether a trade clears the floor —
# which is the honest answer for most of them most of the time.
_EXOTICS = (
    _fx("USDSEK", 25.0, "exotics", 10.0), _fx("USDNOK", 25.0, "exotics", 10.0),
    _fx("USDDKK", 20.0, "exotics", 6.0), _fx("USDPLN", 25.0, "exotics", 4.0),
    _fx("USDCZK", 30.0, "exotics", 20.0), _fx("USDHUF", 25.0, "exotics", 3.0),
    _fx("USDTRY", 30.0, "exotics", 35.0), _fx("USDZAR", 40.0, "exotics", 15.0),
    _fx("USDMXN", 35.0, "exotics", 15.0), _fx("USDSGD", 3.0, "exotics"),
    _fx("USDHKD", 3.5, "exotics", 6.0), _fx("USDCNH", 6.0, "exotics", 6.0),
    _fx("USDTHB", 25.0, "exotics", 30.0), _fx("USDILS", 25.0, "exotics", 3.0),
    _fx("EURSEK", 25.0, "exotics", 10.0), _fx("EURNOK", 30.0, "exotics", 10.0),
    _fx("EURDKK", 15.0, "exotics", 7.0), _fx("EURPLN", 30.0, "exotics", 4.0),
    _fx("EURCZK", 35.0, "exotics", 22.0), _fx("EURHUF", 30.0, "exotics", 3.5),
    _fx("EURTRY", 40.0, "exotics", 30.0), _fx("EURZAR", 45.0, "exotics", 16.0),
    _fx("EURMXN", 45.0, "exotics", 15.0), _fx("EURSGD", 6.0, "exotics"),
    _fx("GBPSEK", 40.0, "exotics", 12.0), _fx("GBPNOK", 45.0, "exotics", 12.0),
    _fx("GBPZAR", 60.0, "exotics", 20.0), _fx("GBPSGD", 8.0, "exotics"),
    _fx("CHFSEK", 40.0, "exotics", 10.0), _fx("SGDJPY", 6.0, "exotics"),
)


# Metals. Gold's pip is 0.01 and its lot is 100 ounces — treating it as an FX
# pair, as an earlier version of this file did, understated every stop by a
# factor of a hundred and oversized every position by the same.
_METALS = (
    _metal("XAUUSD", 0.01, 2, 30.0, 100.0, 20.0),
    _metal("XAGUSD", 0.001, 3, 30.0, 5_000.0, 3.0),
    _metal("XPTUSD", 0.01, 2, 300.0, 100.0, 15.0),
    _metal("XPDUSD", 0.01, 2, 500.0, 100.0, 15.0),
    _metal("XAUEUR", 0.01, 2, 45.0, 100.0, 20.0),
    _metal("XAUGBP", 0.01, 2, 50.0, 100.0, 20.0),
)


REGISTRY: dict[str, Instrument] = {
    inst.symbol: inst for inst in (*_MAJORS, *_CROSSES, *_EXOTICS, *_METALS)
}

# Named sets, so a config can ask for "all" instead of listing sixty symbols.
# "core" is the honest default: the pairs deep enough that a distant target is
# reachable and the spread does not eat the edge before the setup is tested.
GROUPS: dict[str, tuple[str, ...]] = {
    "majors": tuple(i.symbol for i in _MAJORS),
    "crosses": tuple(i.symbol for i in _CROSSES),
    "exotics": tuple(i.symbol for i in _EXOTICS),
    "metals": tuple(i.symbol for i in _METALS),
    "fx": tuple(i.symbol for i in (*_MAJORS, *_CROSSES, *_EXOTICS)),
    "core": tuple(i.symbol for i in (*_MAJORS, *_CROSSES)) + ("XAUUSD",),
    "all": tuple(REGISTRY),
}


# Why ``stop_scale`` exists.
#
# ``risk.min_stop_pips`` and ``risk.max_stop_pips`` are calibrated on FX majors,
# where a pip is roughly 0.9 parts in ten thousand of the price. That bound does
# not travel: on gold a pip is 0.01 of a ~2,400 price, twenty times smaller in
# relative terms, so an unscaled 60-pip ceiling would clamp every gold stop to
# a quarter of one bar's range and hand back a stop nobody could trade.
#
# The scale is therefore how many pips of this instrument equal one pip of a
# major *as a fraction of price* — 20 for gold, 15 for USDZAR, 1 for anything
# quoted like a major. It is a coarse normaliser, not a volatility model: it
# only widens the window a structural stop may sit in, and never changes where
# the stop itself is placed. Majors and crosses all carry 1.0, so their
# behaviour is exactly what it was before the field existed.
STOP_SCALE_NOTE = (
    "stop bounds are expressed in major-pair pips and scaled per instrument; "
    "see instruments.STOP_SCALE_NOTE"
)


def group_names() -> tuple[str, ...]:
    """The group keywords a config or CLI may use in place of a symbol."""
    return tuple(GROUPS)


def normalise_symbol(symbol: str) -> str:
    """Strip the separators brokers disagree about and upper-case the rest."""
    return symbol.upper().replace("/", "").replace("_", "").replace("-", "").strip()


def get_instrument(symbol: str) -> Instrument:
    """Look up an instrument, falling back to a sane 5-digit FX default.

    Unknown symbols are not an error — the user may trade an exotic we have not
    catalogued — but they get a conservative wide spread so results are not
    flattering by accident.
    """
    key = normalise_symbol(symbol)
    if key in REGISTRY:
        return REGISTRY[key]
    if len(key) < 6:
        raise ConfigError(f"unrecognised symbol {symbol!r}: expected a 6-letter pair")
    return _fx(key, spread_pips=3.0, group="unknown")


def expand_symbols(names: list[str] | tuple[str, ...]) -> list[str]:
    """Resolve a mixed list of group keywords and symbols into concrete symbols.

    ``["majors", "XAUUSD"]`` and ``["all"]`` both work. Order is preserved and
    duplicates are dropped, so listing a group and one of its members twice does
    not scan that pair twice.
    """
    resolved: list[str] = []
    seen: set[str] = set()
    for raw in names:
        key = str(raw).strip().lower()
        candidates = GROUPS.get(key, (normalise_symbol(str(raw)),))
        for symbol in candidates:
            if symbol not in seen:
                seen.add(symbol)
                resolved.append(symbol)
    return resolved


def instruments_in(group: str) -> list[Instrument]:
    """Every instrument in a named group."""
    key = group.strip().lower()
    if key not in GROUPS:
        raise ConfigError(
            f"unknown instrument group {group!r}; expected one of {', '.join(group_names())}"
        )
    return [get_instrument(symbol) for symbol in GROUPS[key]]


def pairs_with_currency(currency: str) -> list[str]:
    """Every registered symbol with this currency on either leg.

    Used by the exposure check: three signals sharing a leg are one bet wearing
    three hats, and this is how that gets spotted.
    """
    code = currency.upper()
    return [s for s, inst in REGISTRY.items() if code in inst.currencies]


def currency_name(code: str) -> str:
    """Plain-English name of a currency code, or the code itself if unknown."""
    return CURRENCY_NAMES.get(code.upper(), code.upper())


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
