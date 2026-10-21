"""Typed data contracts for group-ranking input and output."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any, Mapping

from app.domain.relative_strength import BALANCED_RS_FORMULA_VERSION, GROUP_AVG_RS_FIELDS


@dataclass(frozen=True)
class GroupRankPrefetchStats:
    target_symbols: int
    symbols_with_prices: int
    cache_miss_symbols: int
    cache_miss_symbols_sample: tuple[str, ...]
    cache_coverage_ratio: float
    benchmark_available: bool
    benchmark_cached: bool
    benchmark_symbol: str
    benchmark_role: str
    market: str
    cache_only: bool
    skipped_unsupported_symbols: int

    def to_dict(self) -> dict[str, Any]:
        result = {
            "target_symbols": self.target_symbols,
            "symbols_with_prices": self.symbols_with_prices,
            "cache_miss_symbols": self.cache_miss_symbols,
            "cache_miss_symbols_sample": list(self.cache_miss_symbols_sample),
            "cache_coverage_ratio": self.cache_coverage_ratio,
            "spy_cached": self.benchmark_available,
            "benchmark_cached": self.benchmark_cached,
            "benchmark_symbol": self.benchmark_symbol,
            "benchmark_role": self.benchmark_role,
            "market": self.market,
            "cache_only": self.cache_only,
            "skipped_unsupported_symbols": (self.skipped_unsupported_symbols),
        }
        return result


@dataclass(frozen=True)
class GroupRanking:
    industry_group: str
    date: date
    rank: int
    avg_rs_rating: float
    median_rs_rating: float | None
    weighted_avg_rs_rating: float | None
    rs_std_dev: float | None
    num_stocks: int
    num_stocks_rs_above_80: int
    top_symbol: str | None
    top_rs_rating: float | None
    avg_rs_rating_1d: float | None = None
    avg_rs_rating_1w: float | None = None
    avg_rs_rating_1m: float | None = None
    avg_rs_rating_3m: float | None = None
    avg_rs_rating_6m: float | None = None
    rs_formula_version: str = BALANCED_RS_FORMULA_VERSION
    market_rs_run_id: int | None = None

    @classmethod
    def from_mapping(cls, row: Mapping[str, Any]) -> "GroupRanking":
        return cls(
            industry_group=str(row["industry_group"]),
            date=row["date"],
            rank=int(row["rank"]),
            avg_rs_rating=float(row["avg_rs_rating"]),
            median_rs_rating=row.get("median_rs_rating"),
            weighted_avg_rs_rating=row.get("weighted_avg_rs_rating"),
            rs_std_dev=row.get("rs_std_dev"),
            num_stocks=int(row["num_stocks"]),
            num_stocks_rs_above_80=int(row["num_stocks_rs_above_80"]),
            top_symbol=row.get("top_symbol"),
            top_rs_rating=row.get("top_rs_rating"),
            rs_formula_version=str(
                row.get("rs_formula_version") or BALANCED_RS_FORMULA_VERSION
            ),
            market_rs_run_id=row.get("market_rs_run_id"),
            **{field: row.get(field) for field in GROUP_AVG_RS_FIELDS},
        )

    def to_dict(self) -> dict[str, Any]:
        result = {
            "industry_group": self.industry_group,
            "date": self.date,
            "rank": self.rank,
            "avg_rs_rating": self.avg_rs_rating,
            "median_rs_rating": self.median_rs_rating,
            "weighted_avg_rs_rating": self.weighted_avg_rs_rating,
            "rs_std_dev": self.rs_std_dev,
            "num_stocks": self.num_stocks,
            "num_stocks_rs_above_80": self.num_stocks_rs_above_80,
            "top_symbol": self.top_symbol,
            "top_rs_rating": self.top_rs_rating,
            "rs_formula_version": self.rs_formula_version,
            "market_rs_run_id": self.market_rs_run_id,
        }
        result.update({field: getattr(self, field) for field in GROUP_AVG_RS_FIELDS})
        return result


@dataclass(frozen=True)
class GroupRankCalculationResult:
    rankings: tuple[GroupRanking, ...]
    prefetch_stats: GroupRankPrefetchStats
