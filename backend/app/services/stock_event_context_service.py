"""Event-risk and action-planning helpers for the stock workspace."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any

import pandas as pd
from sqlalchemy.orm import Session

from app.models.institutional_ownership import InstitutionalOwnershipHistory
from app.schemas.strategy_profile import StrategyProfileDetail
from app.services.market_calendar_adapters import CalendarScheduleUnavailable
from app.services.market_calendar_service import MarketCalendarService
from app.services.price_cache_service import PriceCacheService
from app.services.strategy_profile_service import DEFAULT_PROFILE, StrategyProfileService
from app.services.yahoo_earnings_calendar import persisted_next_earnings_date
from app.utils.market_hours import to_eastern_date


def _round_or_none(value: float | None, digits: int = 4) -> float | None:
    if value is None:
        return None
    return round(float(value), digits)


def _normalize_price_history(df: pd.DataFrame | None) -> pd.DataFrame | None:
    if df is None or df.empty:
        return None
    normalized = df.copy()
    normalized.index = pd.to_datetime(normalized.index)
    if getattr(normalized.index, "tz", None) is not None:
        normalized.index = normalized.index.tz_localize(None)
    normalized.index = normalized.index.normalize()
    normalized = normalized.sort_index()
    required = {"Open", "High", "Low", "Close"}
    if not required.issubset(set(normalized.columns)):
        return None
    return normalized


class StockEventContextService:
    """Build deterministic event-risk and regime-action payloads."""

    def __init__(
        self,
        *,
        price_cache: PriceCacheService | None = None,
        profile_service: StrategyProfileService | None = None,
        yfinance_service: Any = None,
        market_calendar: MarketCalendarService | None = None,
    ) -> None:
        from app.wiring.bootstrap import (
            get_market_calendar_service,
            get_price_cache,
            get_yfinance_service,
        )

        self._price_cache = price_cache or get_price_cache()
        self._yfinance_service = yfinance_service or get_yfinance_service()
        self._profile_service = profile_service or StrategyProfileService()
        self._market_calendar = market_calendar or get_market_calendar_service()

    def build(
        self,
        db: Session,
        *,
        symbol: str,
        as_of_date: date | None = None,
        regime_label: str | None = None,
        profile: str | None = None,
        fundamentals: dict[str, Any] | None = None,
        market: str | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        today = to_eastern_date(datetime.now(UTC))
        effective_as_of_date = as_of_date or today
        profile_detail = self._profile_service.get_profile(profile or DEFAULT_PROFILE)

        # The fundamentals refresh stores the calendar; never ask Yahoo here.
        # Judged against today: the published run can be days older than the
        # latest observation, which would then look like it came from the future.
        next_earnings_date, _observed = persisted_next_earnings_date(
            fundamentals, as_of_date=today
        )
        recent_earnings: list[dict[str, Any]] = (fundamentals or {}).get("recent_earnings") or []
        days_until_earnings = (
            (next_earnings_date - today).days if next_earnings_date is not None else None
        )

        notes: list[str] = []
        if not recent_earnings:
            notes.append("Recent earnings history is unavailable.")
        if next_earnings_date is None:
            notes.append("Upcoming earnings date is unavailable.")

        earnings_window_risk = self._earnings_window_risk(
            days_until_earnings,
            profile_detail=profile_detail,
        )

        avg_gap_pct = avg_return_5s_pct = None
        if recent_earnings:
            price_history = _normalize_price_history(
                self._price_cache.get_cached_only(symbol.upper(), period="2y")
            )
            if price_history is not None:
                # Mid-session the cache can end in a partial bar for today.
                last_session = self._market_calendar.last_completed_trading_day(market)
                price_history = price_history.loc[: pd.Timestamp(last_session)]
                # A bar fetched mid-session stays partial after its close until
                # the post-close refresh rewrites it.
                if self._price_cache._is_intraday_data_stale(symbol.upper(), market=market):
                    price_history = price_history.iloc[:-1]
            avg_gap_pct, avg_return_5s_pct = self._compute_post_earnings_price_effects(
                recent_earnings,
                price_history,
                market,
            )
            if price_history is None:
                notes.append("Cached price history is unavailable for post-earnings drift analysis.")

        ownership_current, ownership_delta_90d = self._load_institutional_ownership(
            db,
            symbol=symbol,
            as_of_date=effective_as_of_date,
        )
        if ownership_current is None and fundamentals:
            ownership_current = fundamentals.get("institutional_ownership")
        if ownership_current is None:
            notes.append("Institutional ownership history is unavailable.")

        # Unknown, not zero, while there is no earnings history.
        beat_count, miss_count = (
            self._count_earnings_surprises(recent_earnings) if recent_earnings else (None, None)
        )
        event_risk = {
            "next_earnings_date": next_earnings_date.isoformat() if next_earnings_date else None,
            "days_until_earnings": days_until_earnings,
            "earnings_window_risk": earnings_window_risk,
            "recent_earnings_count": len(recent_earnings) or None,
            "beat_count_last_4": beat_count,
            "miss_count_last_4": miss_count,
            "avg_post_earnings_gap_pct": _round_or_none(avg_gap_pct),
            "avg_post_earnings_5s_return_pct": _round_or_none(avg_return_5s_pct),
            "institutional_ownership_current": _round_or_none(ownership_current),
            "institutional_ownership_delta_90d": _round_or_none(ownership_delta_90d),
            "notes": notes,
        }
        regime_actions = self._build_regime_actions(
            regime_label=regime_label or "unavailable",
            profile_detail=profile_detail,
            event_risk=event_risk,
        )
        return event_risk, regime_actions

    def get_next_earnings_summary(
        self,
        symbol: str,
        *,
        as_of_date: date | None = None,
    ) -> tuple[date | None, int | None]:
        next_earnings_date, days_until_earnings, _available = (
            self.get_next_earnings_summary_with_status(
                symbol,
                as_of_date=as_of_date,
            )
        )
        return next_earnings_date, days_until_earnings

    def get_next_earnings_summary_with_status(
        self,
        symbol: str,
        *,
        as_of_date: date | None = None,
    ) -> tuple[date | None, int | None, bool]:
        """Return the next event while preserving provider availability."""
        effective_as_of_date = as_of_date or to_eastern_date(datetime.now(UTC))
        upcoming_dates, available = (
            self._yfinance_service.get_upcoming_earnings_dates_with_status(
                symbol.upper()
            )
        )
        if not available:
            return None, None, False
        next_earnings_date = self._select_next_earnings_date(
            upcoming_dates,
            effective_as_of_date,
        )
        if next_earnings_date is None:
            return None, None, True
        return (
            next_earnings_date,
            (next_earnings_date - effective_as_of_date).days,
            True,
        )

    def _select_next_earnings_date(
        self,
        earnings_dates: list[date],
        as_of_date: date,
    ) -> date | None:
        normalized = sorted({value for value in earnings_dates if value is not None})
        for earnings_date in normalized:
            if earnings_date >= as_of_date:
                return earnings_date
        return None

    def _earnings_window_risk(
        self,
        days_until_earnings: int | None,
        *,
        profile_detail: StrategyProfileDetail,
    ) -> str:
        if days_until_earnings is None:
            return "safe"
        if days_until_earnings <= profile_detail.stock_action.earnings_imminent_days:
            return "imminent"
        if days_until_earnings <= profile_detail.stock_action.earnings_caution_days:
            return "caution"
        return "safe"

    def _compute_post_earnings_price_effects(
        self,
        earnings: list[dict[str, Any]],
        price_history: pd.DataFrame | None,
        market: str | None,
    ) -> tuple[float | None, float | None]:
        if price_history is None or price_history.empty or not earnings:
            return None, None

        exchange_tz = self._market_calendar.market_timezone(market)
        gap_values: list[float] = []
        return_values: list[float] = []
        for item in earnings:
            reported_at = pd.Timestamp(item["report_ts"]).tz_convert(exchange_tz)
            report_day = pd.Timestamp(reported_at.date())
            try:
                session_open = self._market_calendar.session_open(market, reported_at.date())
            except CalendarScheduleUnavailable:
                continue
            # A report before that day's open moves that session; one at or
            # after the open (intraday or after the close) moves the next.
            if session_open is not None and reported_at < session_open:
                trading_days = price_history.index[price_history.index >= report_day]
            else:
                trading_days = price_history.index[price_history.index > report_day]
            if len(trading_days) == 0:
                continue
            entry_day = trading_days[0]
            entry_row = price_history.loc[entry_day]
            entry_open = float(entry_row["Open"])
            prior_days = price_history.index[price_history.index < entry_day]
            if entry_open > 0 and len(prior_days) > 0:
                previous_close = float(price_history.loc[prior_days[-1]]["Close"])
                if previous_close > 0:
                    gap_values.append(((entry_open / previous_close) - 1.0) * 100.0)
            if entry_open <= 0:
                continue
            window_days = trading_days[:5]
            if len(window_days) < 5:
                continue
            final_close = float(price_history.loc[window_days[-1]]["Close"])
            return_values.append(((final_close / entry_open) - 1.0) * 100.0)

        avg_gap = sum(gap_values) / len(gap_values) if gap_values else None
        avg_return = sum(return_values) / len(return_values) if return_values else None
        return avg_gap, avg_return

    def _count_earnings_surprises(
        self,
        earnings: list[dict[str, Any]],
    ) -> tuple[int, int]:
        beat_count = 0
        miss_count = 0
        for item in earnings:
            surprise_pct = item.get("surprise_pct")
            if surprise_pct is None:
                continue
            if surprise_pct > 0:
                beat_count += 1
            elif surprise_pct < 0:
                miss_count += 1
        return beat_count, miss_count

    def _load_institutional_ownership(
        self,
        db: Session,
        *,
        symbol: str,
        as_of_date: date,
    ) -> tuple[float | None, float | None]:
        rows = (
            db.query(InstitutionalOwnershipHistory)
            .filter(
                InstitutionalOwnershipHistory.symbol == symbol.upper(),
                InstitutionalOwnershipHistory.valid_from <= as_of_date,
            )
            .order_by(
                InstitutionalOwnershipHistory.valid_from.desc(),
                InstitutionalOwnershipHistory.id.desc(),
            )
            .all()
        )
        if not rows:
            return None, None

        current_row = rows[0]
        baseline_cutoff = as_of_date - timedelta(days=90)
        baseline_row = next(
            (row for row in rows if row.valid_from <= baseline_cutoff and row.institutional_pct is not None),
            None,
        )
        current_pct = current_row.institutional_pct
        if current_pct is None:
            return None, None
        delta_90d = None
        if baseline_row is not None and baseline_row.institutional_pct is not None:
            delta_90d = current_pct - baseline_row.institutional_pct
        return current_pct, delta_90d

    def _build_regime_actions(
        self,
        *,
        regime_label: str,
        profile_detail: StrategyProfileDetail,
        event_risk: dict[str, Any],
    ) -> dict[str, Any]:
        action_config = profile_detail.stock_action
        if regime_label == "offense":
            sizing_guidance = action_config.offense_sizing_guidance
        elif regime_label == "balanced":
            sizing_guidance = action_config.balanced_sizing_guidance
        elif regime_label == "defense":
            sizing_guidance = action_config.defense_sizing_guidance
        else:
            sizing_guidance = "probe"

        caution_flags: list[str] = []
        earnings_window_risk = event_risk.get("earnings_window_risk")
        if regime_label == "defense":
            caution_flags.append("Market regime is defensive.")
        if earnings_window_risk == "caution":
            caution_flags.append("Earnings are inside the caution window.")
        if earnings_window_risk == "imminent":
            caution_flags.append("Earnings are imminent.")
            sizing_guidance = "avoid"
        ownership_delta = event_risk.get("institutional_ownership_delta_90d")
        if ownership_delta is not None and ownership_delta < 0:
            caution_flags.append("Institutional ownership has faded over the last 90 days.")

        avoid_new_entries = sizing_guidance == "avoid" or earnings_window_risk == "imminent"
        emphasis = profile_detail.stock_action.summary_emphasis
        summary = (
            f"{profile_detail.label} profile ({emphasis}) favors {sizing_guidance} sizing while the market stance is "
            f"{regime_label}."
        )
        if avoid_new_entries:
            summary += " New entries should wait for cleaner risk conditions."
        elif caution_flags:
            summary += f" Main caution: {caution_flags[0]}"
        else:
            summary += " Conditions support selective execution."

        return {
            "stance": regime_label,
            "sizing_guidance": sizing_guidance,
            "avoid_new_entries": avoid_new_entries,
            "preferred_setups": list(profile_detail.stock_action.preferred_setups),
            "caution_flags": caution_flags,
            "summary": summary,
        }
