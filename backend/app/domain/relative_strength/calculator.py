from __future__ import annotations

import math
from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from typing import Mapping

BALANCED_RS_FORMULA_VERSION = "balanced-horizon-percentile-v2"


@dataclass(frozen=True)
class RsHorizon:
    key: str
    sessions: int
    composite_weight: float | None = None
    scanner_field: str | None = None
    group_avg_field: str | None = None


RS_HORIZONS = (
    RsHorizon("1d", 1, group_avg_field="avg_rs_rating_1d"),
    RsHorizon("1w", 5, group_avg_field="avg_rs_rating_1w"),
    RsHorizon(
        "1m",
        21,
        composite_weight=0.20,
        scanner_field="rs_rating_1m",
        group_avg_field="avg_rs_rating_1m",
    ),
    RsHorizon(
        "3m",
        63,
        composite_weight=0.30,
        scanner_field="rs_rating_3m",
        group_avg_field="avg_rs_rating_3m",
    ),
    RsHorizon("6m", 126, composite_weight=0.20, group_avg_field="avg_rs_rating_6m"),
    RsHorizon("9m", 189, composite_weight=0.15),
    RsHorizon("12m", 252, composite_weight=0.15, scanner_field="rs_rating_12m"),
)
HORIZON_SESSIONS = {horizon.key: horizon.sessions for horizon in RS_HORIZONS}
HORIZON_WEIGHTS = {
    horizon.key: horizon.composite_weight
    for horizon in RS_HORIZONS
    if horizon.composite_weight is not None
}
HORIZONS = tuple(HORIZON_SESSIONS)
COMPOSITE_HORIZONS = tuple(HORIZON_WEIGHTS)
STOCK_RS_RATING_ATTR_BY_HORIZON = {
    horizon.key: f"rs_{horizon.key}" for horizon in RS_HORIZONS
}
SCANNER_RS_FIELD_BY_HORIZON = {
    horizon.key: horizon.scanner_field
    for horizon in RS_HORIZONS
    if horizon.scanner_field is not None
}
GROUP_AVG_RS_FIELD_BY_HORIZON = {
    horizon.key: horizon.group_avg_field
    for horizon in RS_HORIZONS
    if horizon.group_avg_field is not None
}
GROUP_AVG_RS_FIELDS = tuple(GROUP_AVG_RS_FIELD_BY_HORIZON.values())
SCAN_ROW_RS_FIELDS = ("rs_rating", *SCANNER_RS_FIELD_BY_HORIZON.values())


@dataclass(frozen=True)
class StockRsScore:
    symbol: str
    overall_rs: int
    rs_1d: int
    rs_1w: int
    rs_1m: int
    rs_3m: int
    rs_6m: int
    rs_9m: int
    rs_12m: int
    weighted_composite: float
    excess_return_1d: float
    excess_return_1w: float
    excess_return_1m: float
    excess_return_3m: float
    excess_return_6m: float
    excess_return_9m: float
    excess_return_12m: float

    def as_scanner_fields(self) -> dict[str, int]:
        fields = {
            "rs_rating": self.overall_rs,
        }
        fields.update(
            {
                scanner_field: getattr(
                    self,
                    STOCK_RS_RATING_ATTR_BY_HORIZON[horizon],
                )
                for horizon, scanner_field in SCANNER_RS_FIELD_BY_HORIZON.items()
            }
        )
        return fields


def _rating_for_rank(average_rank: float, count: int) -> int:
    return int(1 + math.floor(98.0 * (average_rank - 1.0) / (count - 1) + 0.5))


def percentile_ratings(values: Mapping[str, float]) -> dict[str, int]:
    if len(values) < 2:
        raise ValueError("percentile ratings require at least two observations")
    if any(not math.isfinite(float(value)) for value in values.values()):
        raise ValueError("percentile inputs must be finite")

    ordered = sorted((float(value), symbol) for symbol, value in values.items())
    result: dict[str, int] = {}
    index = 0
    count = len(ordered)
    while index < count:
        end = index
        while end + 1 < count and ordered[end + 1][0] == ordered[index][0]:
            end += 1
        average_rank = ((index + 1) + (end + 1)) / 2.0
        rating = _rating_for_rank(average_rank, count)
        for position in range(index, end + 1):
            result[ordered[position][1]] = rating
        index = end + 1
    return result


@dataclass(frozen=True)
class StockRsPlacement:
    """A young stock's ratings on the leading horizons it has, placed against
    the eligible universe. Both maps cover the same non-empty proper prefix of
    HORIZONS, so a placement always rates 1d and never 12m, and it has no
    overall rating by construction."""

    symbol: str
    ratings: Mapping[str, int]
    excess_returns: Mapping[str, float]

    def __post_init__(self) -> None:
        count = len(self.ratings)
        if not 1 <= count < len(HORIZONS):
            raise ValueError(
                f"{self.symbol} placement must rate between 1 and "
                f"{len(HORIZONS) - 1} horizons, got {count}"
            )
        prefix = HORIZONS[:count]
        if set(self.ratings) != set(prefix):
            raise ValueError(
                f"{self.symbol} placement horizons {sorted(self.ratings)} are not "
                f"the leading prefix {list(prefix)}"
            )
        if set(self.excess_returns) != set(prefix):
            raise ValueError(
                f"{self.symbol} placement excess returns do not match its ratings"
            )
        ratings = {horizon: self.ratings[horizon] for horizon in prefix}
        if any(not 1 <= rating <= 99 for rating in ratings.values()):
            raise ValueError(f"{self.symbol} placement has an out-of-range rating")
        returns = {horizon: float(self.excess_returns[horizon]) for horizon in prefix}
        if any(not math.isfinite(value) for value in returns.values()):
            raise ValueError(f"{self.symbol} placement has a non-finite excess return")
        object.__setattr__(self, "ratings", ratings)
        object.__setattr__(self, "excess_returns", returns)

    def scanner_fields(self) -> dict[str, int]:
        return {
            scanner_field: self.ratings[horizon]
            for horizon, scanner_field in SCANNER_RS_FIELD_BY_HORIZON.items()
            if horizon in self.ratings
        }


def place_stock_rs(
    young_excess_returns_by_symbol: Mapping[str, Mapping[str, float]],
    *,
    against: Mapping[str, Mapping[str, float]],
) -> dict[str, StockRsPlacement]:
    """Rate each young stock on its leading horizons against the eligible set.

    On a horizon, a young stock gets exactly the rating ``percentile_ratings``
    would give it if it alone joined the eligible values. Each young stock is
    placed on its own and ``against`` is only read, so young stocks never move
    each other and no eligible rating can move.
    """
    if len(against) < 2:
        raise ValueError("stock RS placement requires at least two eligible stocks")
    overlap = sorted(set(young_excess_returns_by_symbol) & set(against))
    if overlap:
        raise ValueError(f"symbols are both eligible and young: {', '.join(overlap)}")
    count = len(against)
    reference: dict[str, list[float]] = {}
    for horizon in HORIZONS:
        values = sorted(float(returns[horizon]) for returns in against.values())
        if any(not math.isfinite(value) for value in values):
            raise ValueError(f"eligible {horizon} excess returns must be finite")
        reference[horizon] = values

    placements: dict[str, StockRsPlacement] = {}
    for symbol, returns in young_excess_returns_by_symbol.items():
        ratings: dict[str, int] = {}
        for horizon in HORIZONS:
            if horizon not in returns:
                break
            value = float(returns[horizon])
            low = bisect_left(reference[horizon], value)
            high = bisect_right(reference[horizon], value)
            # In the eligible set plus this stock, the value and its ties
            # occupy ranks low+1 .. high+1; ties average as percentile_ratings does.
            ratings[horizon] = _rating_for_rank(((low + 1) + (high + 1)) / 2.0, count + 1)
        placements[symbol] = StockRsPlacement(
            symbol,
            ratings,
            {horizon: float(returns[horizon]) for horizon in ratings},
        )
    return placements


def calculate_balanced_rs(
    excess_returns_by_symbol: Mapping[str, Mapping[str, float]],
) -> dict[str, StockRsScore]:
    if len(excess_returns_by_symbol) < 2:
        raise ValueError("balanced RS requires at least two eligible stocks")
    expected = set(HORIZONS)
    normalized: dict[str, dict[str, float]] = {}
    for symbol, values in excess_returns_by_symbol.items():
        missing = expected - set(values)
        if missing:
            raise ValueError(f"{symbol} missing horizons: {', '.join(sorted(missing))}")
        normalized[symbol] = {horizon: float(values[horizon]) for horizon in HORIZONS}

    components = {
        horizon: percentile_ratings(
            {symbol: values[horizon] for symbol, values in normalized.items()}
        )
        for horizon in HORIZONS
    }
    composites = {
        symbol: sum(
            HORIZON_WEIGHTS[horizon] * components[horizon][symbol]
            for horizon in COMPOSITE_HORIZONS
        )
        for symbol in normalized
    }
    overall = percentile_ratings(composites)

    return {
        symbol: StockRsScore(
            symbol=symbol,
            overall_rs=overall[symbol],
            rs_1d=components["1d"][symbol],
            rs_1w=components["1w"][symbol],
            rs_1m=components["1m"][symbol],
            rs_3m=components["3m"][symbol],
            rs_6m=components["6m"][symbol],
            rs_9m=components["9m"][symbol],
            rs_12m=components["12m"][symbol],
            weighted_composite=composites[symbol],
            excess_return_1d=normalized[symbol]["1d"],
            excess_return_1w=normalized[symbol]["1w"],
            excess_return_1m=normalized[symbol]["1m"],
            excess_return_3m=normalized[symbol]["3m"],
            excess_return_6m=normalized[symbol]["6m"],
            excess_return_9m=normalized[symbol]["9m"],
            excess_return_12m=normalized[symbol]["12m"],
        )
        for symbol in normalized
    }
