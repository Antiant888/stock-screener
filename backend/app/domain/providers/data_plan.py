"""Executable provider plans for market/dataset fetches.

The registry owns provider order, fallback shape, batching defaults, and
provenance metadata. Callers should ask for a plan rather than embedding
market/provider branching locally.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from app.domain.markets.market import SUPPORTED_MARKET_CODES

DATASET_FUNDAMENTALS = "fundamentals"
DATASET_PRICES = "prices"
PLAN_VERSION = "2026.10.20.1"

PROVIDER_AKSHARE = "akshare"
PROVIDER_BAOSTOCK = "baostock"
PROVIDER_FINVIZ = "finviz"
PROVIDER_KRX = "krx"
PROVIDER_OPENDART = "opendart"
PROVIDER_SINA = "sina"
PROVIDER_YAHOO_QUOTE = "yahoo_quote"
PROVIDER_YFINANCE = "yfinance"

_DEFAULT_MARKET = "US"


@dataclass(frozen=True, slots=True)
class ProviderPlanStep:
    """One executable provider step in a market/dataset plan."""

    provider: str
    batch_size: int | None = None
    fallback: bool = True

    def __post_init__(self) -> None:
        provider = str(self.provider or "").strip().lower()
        if not provider:
            raise ValueError("provider is required")
        object.__setattr__(self, "provider", provider)
        if self.batch_size is not None and self.batch_size <= 0:
            raise ValueError("batch_size must be positive when provided")


@dataclass(frozen=True, slots=True)
class ProviderDataPlan:
    """Resolved provider plan for one Market/dataset and optional MIC."""

    market: str
    dataset: str
    steps: tuple[ProviderPlanStep, ...]
    version: str = PLAN_VERSION
    mic: str | None = None

    @property
    def providers(self) -> tuple[str, ...]:
        return tuple(step.provider for step in self.steps)

    def allows(self, provider: str) -> bool:
        return str(provider or "").strip().lower() in self.providers

    def step_for(self, provider: str) -> ProviderPlanStep:
        normalized = str(provider or "").strip().lower()
        for step in self.steps:
            if step.provider == normalized:
                return step
        raise KeyError(f"Provider {provider!r} is not in the {self.market}/{self.dataset} plan")

    def provenance_metadata(self) -> dict[str, object]:
        return {
            "version": self.version,
            "dataset": self.dataset,
            "market": self.market,
            "mic": self.mic,
            "providers": list(self.providers),
        }


class ProviderDataPlanRegistry:
    """Resolve provider execution plans by Market, MIC, and dataset."""

    def __init__(
        self,
        *,
        plans: Mapping[tuple[str, str], tuple[ProviderPlanStep, ...]],
        overrides: Mapping[tuple[str, str, str], tuple[ProviderPlanStep, ...]] | None = None,
        version: str = PLAN_VERSION,
    ) -> None:
        self._version = version
        self._plans = {
            self._plan_key(market, dataset): tuple(steps)
            for (market, dataset), steps in plans.items()
        }
        self._overrides = {
            self._override_key(market, mic, dataset): tuple(steps)
            for (market, mic, dataset), steps in (overrides or {}).items()
        }

    @property
    def version(self) -> str:
        return self._version

    @staticmethod
    def _normalize_dataset(dataset: str) -> str:
        return str(dataset or "").strip().lower()

    @staticmethod
    def _normalize_market(market: str | None) -> str:
        if market is None:
            return _DEFAULT_MARKET
        normalized = str(market).strip().upper()
        return normalized or _DEFAULT_MARKET

    @staticmethod
    def _normalize_mic(mic: str | None) -> str | None:
        if mic is None:
            return None
        normalized = str(mic).strip().upper()
        return normalized or None

    @classmethod
    def _plan_key(cls, market: str | None, dataset: str) -> tuple[str, str]:
        return (cls._normalize_market(market), cls._normalize_dataset(dataset))

    @classmethod
    def _override_key(cls, market: str | None, mic: str, dataset: str) -> tuple[str, str, str]:
        normalized_mic = cls._normalize_mic(mic)
        if normalized_mic is None:
            raise ValueError("mic is required for provider data plan overrides")
        return (
            cls._normalize_market(market),
            normalized_mic,
            cls._normalize_dataset(dataset),
        )

    def plan_for(
        self,
        market: str | None,
        dataset: str,
        *,
        mic: str | None = None,
    ) -> ProviderDataPlan:
        normalized_market = self._normalize_market(market)
        normalized_dataset = self._normalize_dataset(dataset)
        normalized_mic = self._normalize_mic(mic)

        if normalized_market not in SUPPORTED_MARKET_CODES:
            return ProviderDataPlan(
                market=normalized_market,
                dataset=normalized_dataset,
                mic=normalized_mic,
                steps=(),
                version=self._version,
            )

        if normalized_mic is not None:
            override = self._overrides.get(
                (normalized_market, normalized_mic, normalized_dataset)
            )
            if override is not None:
                return ProviderDataPlan(
                    market=normalized_market,
                    dataset=normalized_dataset,
                    mic=normalized_mic,
                    steps=override,
                    version=self._version,
                )

        return ProviderDataPlan(
            market=normalized_market,
            dataset=normalized_dataset,
            mic=None,
            steps=self._plans.get((normalized_market, normalized_dataset), ()),
            version=self._version,
        )

    def supported_markets(self, dataset: str = DATASET_FUNDAMENTALS) -> tuple[str, ...]:
        normalized_dataset = self._normalize_dataset(dataset)
        return tuple(
            sorted(market for market, plan_dataset in self._plans if plan_dataset == normalized_dataset)
        )


def _yf(batch_size: int = 50) -> ProviderPlanStep:
    return ProviderPlanStep(PROVIDER_YFINANCE, batch_size=batch_size)


def _yf_quote_repaired(batch_size: int = 50) -> tuple[ProviderPlanStep, ...]:
    """Yahoo history, then Yahoo quotes for the latest session history lacks.

    Yahoo's daily history publishes a completed session hours late (JP, TW, SG,
    MY, AU indices, DE, CA) or drops it from 00:00 UTC (US); the v7 quote keeps
    reporting it. See services/yahoo_quote_price_repair.py.
    """
    return (_yf(batch_size=batch_size), ProviderPlanStep(PROVIDER_YAHOO_QUOTE, batch_size=100))


provider_data_plan_registry = ProviderDataPlanRegistry(
    plans={
        ("US", DATASET_FUNDAMENTALS): (
            ProviderPlanStep(PROVIDER_FINVIZ, batch_size=1, fallback=False),
            _yf(),
        ),
        ("HK", DATASET_FUNDAMENTALS): (_yf(),),
        ("IN", DATASET_FUNDAMENTALS): (_yf(),),
        ("JP", DATASET_FUNDAMENTALS): (_yf(),),
        ("KR", DATASET_FUNDAMENTALS): (
            ProviderPlanStep(PROVIDER_KRX, batch_size=200, fallback=False),
            ProviderPlanStep(PROVIDER_OPENDART, batch_size=100),
            _yf(),
        ),
        ("TW", DATASET_FUNDAMENTALS): (_yf(),),
        ("CN", DATASET_FUNDAMENTALS): (
            ProviderPlanStep(PROVIDER_AKSHARE, batch_size=500, fallback=False),
            ProviderPlanStep(PROVIDER_BAOSTOCK, batch_size=500),
            _yf(batch_size=25),
        ),
        ("CA", DATASET_FUNDAMENTALS): (_yf(),),
        ("DE", DATASET_FUNDAMENTALS): (_yf(),),
        ("SG", DATASET_FUNDAMENTALS): (_yf(),),
        ("MY", DATASET_FUNDAMENTALS): (_yf(),),
        ("AU", DATASET_FUNDAMENTALS): (_yf(),),
        ("US", DATASET_PRICES): _yf_quote_repaired(batch_size=150),
        # Sina only repairs the latest session Yahoo is missing; see
        # services/hk_sina_price_repair.py.
        ("HK", DATASET_PRICES): (
            _yf(batch_size=50),
            ProviderPlanStep(PROVIDER_SINA, batch_size=1),
        ),
        ("IN", DATASET_PRICES): (_yf(batch_size=50),),
        ("JP", DATASET_PRICES): _yf_quote_repaired(),
        ("KR", DATASET_PRICES): (
            ProviderPlanStep(PROVIDER_KRX, batch_size=200, fallback=False),
            _yf(batch_size=50),
        ),
        ("TW", DATASET_PRICES): _yf_quote_repaired(),
        ("CN", DATASET_PRICES): (
            ProviderPlanStep(PROVIDER_AKSHARE, batch_size=500, fallback=False),
            ProviderPlanStep(PROVIDER_BAOSTOCK, batch_size=500),
            _yf(batch_size=25),
        ),
        ("CA", DATASET_PRICES): _yf_quote_repaired(),
        ("DE", DATASET_PRICES): _yf_quote_repaired(),
        ("SG", DATASET_PRICES): _yf_quote_repaired(),
        ("MY", DATASET_PRICES): _yf_quote_repaired(),
        ("AU", DATASET_PRICES): _yf_quote_repaired(),
    },
    overrides={
        ("CN", "XBSE", DATASET_FUNDAMENTALS): (
            ProviderPlanStep(PROVIDER_AKSHARE, batch_size=500, fallback=False),
            ProviderPlanStep(PROVIDER_BAOSTOCK, batch_size=500),
        ),
        ("CN", "XBSE", DATASET_PRICES): (
            ProviderPlanStep(PROVIDER_AKSHARE, batch_size=500, fallback=False),
            ProviderPlanStep(PROVIDER_BAOSTOCK, batch_size=500),
        ),
    },
)
