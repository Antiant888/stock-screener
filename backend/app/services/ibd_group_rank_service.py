"""
Service for calculating and managing IBD Industry Group Rankings.

Each ranking is the per-group average of the published balanced Market RS
snapshot for one market and trading date, stored in the IBDGroupRank table.
"""

import logging
from datetime import date, datetime, timedelta
from typing import Dict, Iterator, List

from sqlalchemy.orm import Session

from app.domain.relative_strength import (
    BALANCED_RS_FORMULA_VERSION,
    GROUP_AVG_RS_FIELDS,
    GroupSnapshotIdentity,
    RsPublicationIdentity,
)
from app.infra.db.repositories.market_rs_repo import MarketRsRunRepository

from ..models.industry import IBDGroupRank
from .canonical_group_ranking_service import CanonicalGroupRankingService
from .derived_data_execution_policy import DerivedDataExecutionPolicy
from .group_constituent_source import (
    GroupConstituentPublicationUnavailable,
    GroupConstituentSource,
)
from .group_detail_payloads import constituent_stock_payloads_from_scan_items
from .group_rank_history_policy import CALENDAR_DAY_GROUP_RANK_CHANGE_WINDOWS
from .group_rank_input_sources import GroupRankUniverseSource
from .group_rank_models import (
    GroupRankCalculationResult,
    GroupRanking,
    GroupRankPrefetchStats,
)
from .group_ranking_history import (
    apply_calendar_rank_changes,
    build_group_detail_payload_from_parts,
)
from .group_ranking_payloads import rank_record_payload
from .group_ranking_repository import GroupRankingRepository
from .market_calendar_service import MarketCalendarService
from .market_rs_snapshot_service import MarketRsSnapshotService

logger = logging.getLogger(__name__)


class MissingIBDIndustryMappingsError(RuntimeError):
    """Raised when tracked IBD industry mappings have not been loaded."""

    def __init__(self) -> None:
        super().__init__("IBD industry mappings are not loaded")


def pct_rs_above_80(count_above_80: int | None, total_count: int | None) -> float | None:
    if not total_count:
        return None
    return round(((count_above_80 or 0) / total_count) * 100, 1)


def _calendar_days(start_date: date, end_date: date) -> Iterator[date]:
    cursor = start_date
    while cursor <= end_date:
        yield cursor
        cursor += timedelta(days=1)


class IBDGroupRankService:
    """
    Service for calculating and managing IBD Industry Group Rankings.

    Methodology:
    1. Publish the balanced Market RS snapshot for the market and date
    2. Average each group's constituent ratings from that snapshot
    3. Rank all groups from highest to lowest avg_rs
    4. Store the daily snapshot in the IBDGroupRank table
    """

    def __init__(
        self,
        *,
        canonical_group_service: CanonicalGroupRankingService,
        market_rs_repository: MarketRsRunRepository,
        market_rs_snapshot_service: MarketRsSnapshotService,
        universe_source: GroupRankUniverseSource,
        calendar_service: MarketCalendarService,
        group_constituent_source: GroupConstituentSource | None = None,
        ranking_repository: GroupRankingRepository | None = None,
    ):
        self.canonical_group_service = canonical_group_service
        self.market_rs_repository = market_rs_repository
        self.market_rs_snapshot_service = market_rs_snapshot_service
        self.universe_source = universe_source
        self.calendar_service = calendar_service
        self.group_constituent_source = (
            group_constituent_source or GroupConstituentSource()
        )
        self.ranking_repository = ranking_repository or GroupRankingRepository()

    def calculate_group_rankings(
        self,
        db: Session,
        calculation_date: date | None = None,
        *,
        market: str | None = None,
        policy: DerivedDataExecutionPolicy = (
            DerivedDataExecutionPolicy.provider_allowed()
        ),
    ) -> GroupRankCalculationResult:
        """Publish the Market RS run for the date and store its group averages."""
        if calculation_date is None:
            calculation_date = datetime.now().date()
        normalized_market = (market or "US").upper()
        run = self.market_rs_snapshot_service.calculate(
            db,
            market=normalized_market,
            as_of_date=calculation_date,
            formula_version=BALANCED_RS_FORMULA_VERSION,
            rebuild_incompatible=True,
        )
        rows = self.canonical_group_service.calculate_and_store(
            db,
            market=normalized_market,
            as_of_date=calculation_date,
            formula_version=BALANCED_RS_FORMULA_VERSION,
        )
        expected = int(run.expected_symbol_count or 0)
        eligible = int(run.eligible_symbol_count or 0)
        stats = GroupRankPrefetchStats(
            target_symbols=expected,
            symbols_with_prices=eligible,
            cache_miss_symbols=0,
            cache_miss_symbols_sample=(),
            cache_coverage_ratio=(eligible / expected if expected else 1.0),
            benchmark_available=True,
            benchmark_cached=True,
            benchmark_symbol=str(run.benchmark_symbol),
            benchmark_role="market_rs_snapshot",
            market=normalized_market,
            cache_only=policy.cache_only,
            skipped_unsupported_symbols=int(run.excluded_symbol_count or 0),
        )
        return GroupRankCalculationResult(
            rankings=tuple(GroupRanking.from_mapping(row) for row in rows),
            prefetch_stats=stats,
        )

    def get_current_rankings(
        self,
        db: Session,
        limit: int = 197,
        calculation_date: date | None = None,
        *,
        market: str = "US",
    ) -> List[Dict]:
        """
        Get most recent group rankings with rank changes for one market.

        Rankings are partitioned by market — each market maintains its own
        rank ordering (both US and HK can have a rank #1), so every query
        must filter by market to avoid mixing them.
        """
        normalized_market = (market or "US").upper()
        rankings = self.ranking_repository.current_rank_rows(
            db,
            limit=limit,
            market=normalized_market,
            calculation_date=calculation_date,
        )

        if not rankings:
            return []
        latest_date = rankings[0].date

        # Calendar-day target offsets for rank changes. The lookup below picks
        # the closest stored ranking within a small window; these are not exact
        # trading-session offsets.
        period_days = dict(CALENDAR_DAY_GROUP_RANK_CHANGE_WINDOWS)

        # Batch fetch all historical ranks in ONE query instead of 197*4=788 queries
        group_names = [r.industry_group for r in rankings]
        historical_ranks = self.ranking_repository.historical_ranks_batch(
            db,
            group_names=group_names,
            current_date=latest_date,
            period_days=period_days,
            market=normalized_market,
        )

        result = [
            self._rank_record_payload(
                ranking,
                pct_rs_above_80=pct_rs_above_80(
                    ranking.num_stocks_rs_above_80, ranking.num_stocks
                ),
            )
            for ranking in rankings
        ]

        apply_calendar_rank_changes(
            result,
            historical_ranks=historical_ranks,
            periods=period_days,
        )
        self._annotate_top_symbol_names(db, result)
        return result

    @staticmethod
    def _rank_record_payload(
        ranking: IBDGroupRank,
        *,
        pct_rs_above_80: float | None,
        top_symbol_name: str | None = None,
    ) -> Dict:
        return rank_record_payload(
            ranking,
            pct_rs_above_80=pct_rs_above_80,
            top_symbol_name=top_symbol_name,
        )

    def _annotate_top_symbol_names(self, db: Session, rows: List[Dict]) -> None:
        name_map = self._get_symbol_name_map(
            db,
            [row.get("top_symbol") for row in rows],
        )
        for row in rows:
            row["top_symbol_name"] = name_map.get(row.get("top_symbol"))

    def _get_symbol_name_map(
        self,
        db: Session,
        symbols: List[str | None],
    ) -> Dict[str, str | None]:
        normalized_symbols = {
            str(symbol).strip() for symbol in symbols if str(symbol or "").strip()
        }
        if not normalized_symbols:
            return {}
        return self.universe_source.symbol_names(db, sorted(normalized_symbols))

    def get_historical_ranks_batch(
        self,
        db: Session,
        group_names: List[str],
        current_date: date,
        period_days: Dict[str, int],
        *,
        market: str = "US",
    ) -> Dict[tuple, int]:
        """Return closest historical ranks for each group and period."""
        return self.ranking_repository.historical_ranks_batch(
            db,
            group_names=group_names,
            current_date=current_date,
            period_days=period_days,
            market=market,
        )

    def get_group_history(
        self,
        db: Session,
        industry_group: str,
        days: int = 180,
        *,
        market: str = "US",
    ) -> Dict:
        """Get historical ranking data for a specific group, scoped by market."""
        normalized_market = (market or "US").upper()
        cutoff_date = datetime.now().date() - timedelta(days=days)

        records = self.ranking_repository.group_rank_rows(
            db,
            industry_group=industry_group,
            start_date=cutoff_date,
            market=normalized_market,
        )

        if not records:
            return {"industry_group": industry_group, "history": []}

        # Current (most recent) data
        current = records[0]

        # Get rank changes using calendar-day target offsets with closest-record matching.
        period_days = dict(CALENDAR_DAY_GROUP_RANK_CHANGE_WINDOWS)

        historical_ranks = self.ranking_repository.historical_ranks_batch(
            db,
            group_names=[industry_group],
            current_date=current.date,
            period_days=period_days,
            market=normalized_market,
        )
        rank_changes = {
            "industry_group": industry_group,
            "rank": current.rank,
        }
        apply_calendar_rank_changes(
            [rank_changes],
            historical_ranks=historical_ranks,
            periods=period_days,
        )

        # Build history data points
        history = [
            {
                "date": r.date.isoformat(),
                "rank": r.rank,
                "avg_rs_rating": r.avg_rs_rating,
                "num_stocks": r.num_stocks,
                **{field: getattr(r, field) for field in GROUP_AVG_RS_FIELDS},
            }
            for r in records
        ]

        # Get constituent stocks with metrics
        stocks = self._get_constituent_stocks(
            db,
            industry_group,
            ranking=current,
        )

        top_symbol_name = self._get_symbol_name_map(db, [current.top_symbol]).get(
            current.top_symbol
        )
        ranking_payload = self._rank_record_payload(
            current,
            pct_rs_above_80=pct_rs_above_80(
                current.num_stocks_rs_above_80, current.num_stocks
            ),
            top_symbol_name=top_symbol_name,
        )
        ranking_payload.update(rank_changes)

        return build_group_detail_payload_from_parts(
            industry_group,
            ranking=ranking_payload,
            history=history,
            stocks=stocks,
        )

    def _get_constituent_stocks(
        self,
        db: Session,
        industry_group: str,
        *,
        ranking: IBDGroupRank,
    ) -> List[Dict]:
        """
        Get constituent stocks for an industry group with their metrics.

        Fetches from the Feature run published for the ranking's exact
        Market RS run.
        """
        snapshot = GroupSnapshotIdentity(
            ranking.market,
            ranking.date,
            ranking.rs_formula_version,
        )
        run = self.market_rs_repository.get_completed_exact(
            db,
            market=snapshot.market,
            as_of_date=snapshot.as_of_date,
            formula_version=snapshot.formula_version,
            load_rows=False,
        )
        if (
            run is None
            or run.id != ranking.market_rs_run_id
            or run.eligible_symbol_count <= 0
        ):
            raise GroupConstituentPublicationUnavailable(
                "Canonical Group rank no longer resolves to its exact "
                "Market RS publication."
            )
        publication = RsPublicationIdentity(
            snapshot=snapshot,
            market_rs_run_id=run.id,
            universe_size=run.eligible_symbol_count,
        )
        items = self.group_constituent_source.get_constituent_items(
            db,
            industry_group,
            publication=publication,
        )
        return constituent_stock_payloads_from_scan_items(items)

    def get_rank_movers(
        self,
        db: Session,
        period: str = "1w",
        limit: int = 20,
        calculation_date: date | None = None,
        *,
        market: str = "US",
    ) -> Dict:
        """Get groups with biggest rank changes over a period, scoped by market."""
        current_rankings = self.get_current_rankings(
            db,
            limit=197,
            calculation_date=calculation_date,
            market=market,
        )

        if not current_rankings:
            return {"period": period, "gainers": [], "losers": []}

        # Filter to groups with rank change data for this period
        change_key = f"rank_change_{period}"
        groups_with_change = [
            r for r in current_rankings if r.get(change_key) is not None
        ]

        # Split by sign so a market with only positive (or only negative) movers
        # never reports the smallest gainers as "losers" or vice versa.
        gainers = [r for r in groups_with_change if r[change_key] > 0]
        losers = [r for r in groups_with_change if r[change_key] < 0]
        gainers.sort(key=lambda r: r[change_key], reverse=True)
        losers.sort(key=lambda r: r[change_key])

        return {
            "period": period,
            "gainers": gainers[:limit],
            "losers": losers[:limit],
        }

    def backfill_rankings_optimized(
        self,
        db: Session,
        start_date: date,
        end_date: date,
        *,
        market: str = "US",
    ) -> Dict:
        normalized_market = (market or "US").upper()
        dates = [
            day
            for day in _calendar_days(start_date, end_date)
            if self.calendar_service.is_trading_day(normalized_market, day)
        ]
        result = self.fill_gaps_optimized(db, dates, market=normalized_market)
        return {
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
            "deleted": 0,
            **result,
        }

    def find_missing_dates(
        self,
        db: Session,
        lookback_days: int = 365,
        *,
        market: str = "US",
        end_date: date | None = None,
    ) -> List[date]:
        normalized_market = (market or "US").upper()
        window_end = (
            end_date or self.calendar_service.market_now(normalized_market).date()
        )
        start_date = window_end - timedelta(days=lookback_days)
        existing = self.ranking_repository.existing_dates(
            db,
            start_date=start_date,
            end_date=window_end,
            market=normalized_market,
        )
        return [
            day
            for day in _calendar_days(start_date, window_end - timedelta(days=1))
            if self.calendar_service.is_trading_day(normalized_market, day)
            and day not in existing
        ]

    def fill_gaps_optimized(
        self,
        db: Session,
        missing_dates: List[date],
        *,
        market: str = "US",
        policy: DerivedDataExecutionPolicy = (
            DerivedDataExecutionPolicy.provider_allowed()
        ),
    ) -> Dict:
        processed = 0
        errors = 0
        for calculation_date in missing_dates:
            try:
                result = self.calculate_group_rankings(
                    db,
                    calculation_date,
                    market=market,
                    policy=policy,
                )
                processed += bool(result.rankings)
                errors += not bool(result.rankings)
            except Exception:
                db.rollback()
                errors += 1
                logger.exception(
                    "Canonical Group gap-fill failed for %s",
                    calculation_date,
                )
        return {
            "total_dates": len(missing_dates),
            "processed": processed,
            "skipped": 0,
            "errors": errors,
        }
