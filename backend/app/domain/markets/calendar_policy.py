"""Canonical market calendar provider and session-override policy."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, time
from enum import Enum


class CalendarProvider(Enum):
    """Supported third-party calendar engines."""

    EXCHANGE_CALENDARS = "exchange_calendars"
    PANDAS_MARKET_CALENDARS = "pandas_market_calendars"

    @property
    def package_name(self) -> str:
        return self.value.replace("_", "-")


@dataclass(frozen=True, slots=True)
class CalendarSessionOverride:
    """Authoritative override for a provider-reported market session date."""

    market: str
    day: date
    is_trading_day: bool

    def normalized_market(self) -> str:
        return self.market.strip().upper()


DEFAULT_CALENDAR_SESSION_OVERRIDES: tuple[CalendarSessionOverride, ...] = (
    CalendarSessionOverride("JP", date(2026, 3, 20), False),
    CalendarSessionOverride("JP", date(2026, 9, 22), False),
    CalendarSessionOverride("JP", date(2026, 9, 23), False),
    # Taiwan's May 2025 holiday law added these after the pinned XTAI schedule
    # was cut; TW has no official 2025 manifest, so the provider still trades them.
    CalendarSessionOverride("TW", date(2025, 9, 29), False),
    CalendarSessionOverride("TW", date(2025, 10, 24), False),
    CalendarSessionOverride("TW", date(2025, 12, 25), False),
)


# Used only when an official session is verified but the pinned provider cannot
# return its schedule (for example, because its bundled data ends earlier).
REGULAR_MARKET_OPEN_TIMES: dict[str, time] = {
    "US": time(9, 30),
    "HK": time(9, 30),
    "IN": time(9, 15),
    "JP": time(9, 0),
    "KR": time(9, 0),
    "TW": time(9, 0),
    "CN": time(9, 30),
    "CA": time(9, 30),
    "DE": time(9, 0),
    "SG": time(9, 0),
    "AU": time(10, 0),
    "MY": time(9, 0),
}

REGULAR_MARKET_BREAKS: dict[str, tuple[time, time]] = {
    "CN": (time(11, 30), time(13, 0)),
    "HK": (time(12, 0), time(13, 0)),
    "JP": (time(11, 30), time(12, 30)),
}

REGULAR_MARKET_CLOSE_TIMES: dict[str, time] = {
    "US": time(16, 0),
    "HK": time(16, 0),
    "IN": time(15, 30),
    "JP": time(15, 30),
    "KR": time(15, 30),
    "TW": time(13, 30),
    "CN": time(15, 0),
    "CA": time(16, 0),
    "DE": time(17, 30),
    "SG": time(17, 0),
    "AU": time(16, 0),
    "MY": time(17, 0),
}
