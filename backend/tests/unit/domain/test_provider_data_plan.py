from __future__ import annotations

import pytest

from app.domain.providers.data_plan import (
    DATASET_FUNDAMENTALS,
    DATASET_PRICES,
    PLAN_VERSION,
    ProviderDataPlanRegistry,
    ProviderPlanStep,
    PROVIDER_AKSHARE,
    PROVIDER_BAOSTOCK,
    PROVIDER_KRX,
    PROVIDER_YAHOO_QUOTE,
    provider_data_plan_registry,
)
from app.services.provider_routing_policy import (
    PROVIDER_FINVIZ,
    PROVIDER_YFINANCE,
)


def test_default_fundamentals_plan_preserves_us_provider_order() -> None:
    plan = provider_data_plan_registry.plan_for("US", DATASET_FUNDAMENTALS)

    assert plan.market == "US"
    assert plan.dataset == DATASET_FUNDAMENTALS
    assert plan.mic is None
    assert plan.version == PLAN_VERSION
    assert plan.providers == (
        PROVIDER_FINVIZ,
        PROVIDER_YFINANCE,
    )
    assert plan.step_for(PROVIDER_YFINANCE).batch_size == 50


def test_default_fundamentals_plan_records_provenance_metadata() -> None:
    metadata = provider_data_plan_registry.plan_for("HK", DATASET_FUNDAMENTALS).provenance_metadata()

    assert metadata == {
        "version": PLAN_VERSION,
        "dataset": DATASET_FUNDAMENTALS,
        "market": "HK",
        "mic": None,
        "providers": [PROVIDER_YFINANCE],
    }


def test_au_provider_plan_uses_yfinance_with_quote_repair_for_prices() -> None:
    fundamentals = provider_data_plan_registry.plan_for("AU", DATASET_FUNDAMENTALS)
    prices = provider_data_plan_registry.plan_for("AU", DATASET_PRICES)

    assert fundamentals.providers == (PROVIDER_YFINANCE,)
    assert fundamentals.step_for(PROVIDER_YFINANCE).batch_size == 50
    assert prices.providers == (PROVIDER_YFINANCE, PROVIDER_YAHOO_QUOTE)
    assert prices.step_for(PROVIDER_YFINANCE).batch_size == 50


@pytest.mark.parametrize("market", ["US", "JP", "TW", "SG", "MY", "AU", "DE", "CA"])
def test_yahoo_history_markets_repair_the_latest_session_from_quotes(market) -> None:
    """#479: Yahoo history publishes these markets' latest session late; quotes repair it."""
    prices = provider_data_plan_registry.plan_for(market, DATASET_PRICES)

    assert prices.providers == (PROVIDER_YFINANCE, PROVIDER_YAHOO_QUOTE)
    assert prices.step_for(PROVIDER_YAHOO_QUOTE).batch_size == 100


def test_registry_applies_market_mic_dataset_override() -> None:
    registry = ProviderDataPlanRegistry(
        plans={
            ("US", DATASET_FUNDAMENTALS): (
                ProviderPlanStep(PROVIDER_FINVIZ, batch_size=None),
            ),
        },
        overrides={
            ("US", "XNAS", DATASET_FUNDAMENTALS): (
                ProviderPlanStep(PROVIDER_YFINANCE, batch_size=25),
            ),
        },
        version="test-plan",
    )

    base = registry.plan_for("US", DATASET_FUNDAMENTALS)
    override = registry.plan_for("us", DATASET_FUNDAMENTALS, mic="xnas")

    assert base.providers == (PROVIDER_FINVIZ,)
    assert base.mic is None
    assert override.providers == (PROVIDER_YFINANCE,)
    assert override.mic == "XNAS"
    assert override.version == "test-plan"
    assert override.step_for(PROVIDER_YFINANCE).batch_size == 25


def test_unknown_market_fails_closed_with_empty_plan() -> None:
    plan = provider_data_plan_registry.plan_for("XX", DATASET_FUNDAMENTALS)

    assert plan.market == "XX"
    assert plan.providers == ()


def test_price_plans_record_provider_order_batching_and_provenance() -> None:
    us = provider_data_plan_registry.plan_for("US", DATASET_PRICES)
    kr = provider_data_plan_registry.plan_for("KR", DATASET_PRICES)
    cn = provider_data_plan_registry.plan_for("CN", DATASET_PRICES)

    assert us.providers == (PROVIDER_YFINANCE, PROVIDER_YAHOO_QUOTE)
    assert us.step_for(PROVIDER_YFINANCE).batch_size == 150
    assert kr.providers == (PROVIDER_KRX, PROVIDER_YFINANCE)
    assert kr.step_for(PROVIDER_KRX).batch_size == 200
    assert kr.step_for(PROVIDER_YFINANCE).batch_size == 50
    assert cn.providers == (PROVIDER_AKSHARE, PROVIDER_BAOSTOCK, PROVIDER_YFINANCE)
    assert cn.step_for(PROVIDER_YFINANCE).batch_size == 25
    assert cn.provenance_metadata() == {
        "version": PLAN_VERSION,
        "dataset": DATASET_PRICES,
        "market": "CN",
        "mic": None,
        "providers": [PROVIDER_AKSHARE, PROVIDER_BAOSTOCK, PROVIDER_YFINANCE],
    }


def test_price_plan_mic_override_can_disable_yfinance_fallback() -> None:
    bjse = provider_data_plan_registry.plan_for("CN", DATASET_PRICES, mic="XBSE")

    assert bjse.mic == "XBSE"
    assert bjse.providers == (PROVIDER_AKSHARE, PROVIDER_BAOSTOCK)
    assert not bjse.allows(PROVIDER_YFINANCE)


def test_fundamentals_plan_mic_override_can_disable_yfinance_fallback() -> None:
    bjse = provider_data_plan_registry.plan_for("CN", DATASET_FUNDAMENTALS, mic="XBSE")

    assert bjse.mic == "XBSE"
    assert bjse.providers == (PROVIDER_AKSHARE, PROVIDER_BAOSTOCK)
    assert not bjse.allows(PROVIDER_YFINANCE)
