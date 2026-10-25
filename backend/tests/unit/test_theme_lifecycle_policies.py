"""Tests for lifecycle automation policies and relationship inference."""

from __future__ import annotations

import warnings
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import pytest
from app.database import Base
from app.models.app_settings import AppSetting
from app.models.stock import StockPrice
from app.models.theme import (
    ContentItem,
    ContentSource,
    ThemeAlert,
    ThemeCluster,
    ThemeConstituent,
    ThemeLifecycleTransition,
    ThemeMention,
    ThemeMergeSuggestion,
    ThemeMetrics,
    ThemeRelationship,
)
from app.services.theme_discovery_service import ThemeDiscoveryService
from app.services.theme_equivalence_service import ThemeEquivalenceService
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


@pytest.fixture
def db_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def _make_source(db_session, *, name: str, source_type: str) -> ContentSource:
    source = ContentSource(
        name=name,
        source_type=source_type,
        url=f"https://{name.lower().replace(' ', '-')}.example.com/feed",
        is_active=True,
        pipelines=["technical", "fundamental"],
    )
    db_session.add(source)
    db_session.flush()
    return source


def _make_theme(
    db_session,
    *,
    name: str,
    canonical_key: str,
    state: str,
    now: datetime,
) -> ThemeCluster:
    theme = ThemeCluster(
        name=name,
        canonical_key=canonical_key,
        display_name=name,
        pipeline="technical",
        is_active=True,
        lifecycle_state=state,
        candidate_since_at=now - timedelta(days=7),
        activated_at=(now - timedelta(days=7)) if state in {"active", "reactivated", "dormant"} else None,
        first_seen_at=now - timedelta(days=7),
        last_seen_at=now - timedelta(days=1),
    )
    db_session.add(theme)
    db_session.flush()
    return theme


def _add_mention(
    db_session,
    *,
    theme: ThemeCluster,
    source: ContentSource,
    now: datetime,
    days_ago: int,
    confidence: float,
    external_suffix: str,
) -> None:
    published_at = now - timedelta(days=days_ago)
    content = ContentItem(
        source_id=source.id,
        source_type=source.source_type,
        source_name=source.name,
        external_id=f"{theme.canonical_key}-{external_suffix}",
        title=f"{theme.name} mention {external_suffix}",
        content=f"Evidence for {theme.name}",
        published_at=published_at,
        is_processed=True,
        processed_at=published_at,
    )
    db_session.add(content)
    db_session.flush()
    from app.services.theme_evidence_eligibility_service import grant_eligibility
    grant_eligibility(db_session, content.id, "technical", "legacy", source.id, published_at.replace(tzinfo=timezone.utc))
    mention = ThemeMention(
        content_item_id=content.id,
        source_type=source.source_type,
        source_name=source.name,
        raw_theme=theme.display_name,
        canonical_theme=theme.display_name,
        theme_cluster_id=theme.id,
        pipeline="technical",
        tickers=[],
        ticker_count=0,
        sentiment="bullish",
        confidence=confidence,
        excerpt="Theme evidence",
        mentioned_at=published_at,
    )
    db_session.add(mention)


def _add_stock_price(
    db_session,
    *,
    symbol: str,
    trade_date: datetime,
    close: float | None,
) -> None:
    db_session.add(
        StockPrice(
            symbol=symbol,
            date=trade_date.date(),
            open=close,
            high=close,
            low=close,
            close=close,
            adj_close=close,
            volume=1_000_000,
        )
    )


def test_candidate_promotion_policy_promotes_theme_with_persistent_diverse_evidence(db_session):
    now = datetime(2026, 2, 24, 15, 0, 0)
    source_a = _make_source(db_session, name="Alpha Desk", source_type="news")
    source_b = _make_source(db_session, name="Bravo Research", source_type="substack")
    theme = _make_theme(
        db_session,
        name="AI Infrastructure",
        canonical_key="ai_infrastructure",
        state="candidate",
        now=now,
    )
    _add_mention(db_session, theme=theme, source=source_a, now=now, days_ago=1, confidence=0.92, external_suffix="1")
    _add_mention(db_session, theme=theme, source=source_b, now=now, days_ago=2, confidence=0.88, external_suffix="2")
    _add_mention(db_session, theme=theme, source=source_a, now=now, days_ago=3, confidence=0.89, external_suffix="3")
    _add_mention(db_session, theme=theme, source=source_b, now=now, days_ago=5, confidence=0.91, external_suffix="4")
    db_session.commit()

    service = ThemeDiscoveryService(db_session, pipeline="technical")
    result = service.promote_candidate_themes(now=now)

    db_session.refresh(theme)
    assert result["promoted"] == 1
    assert theme.lifecycle_state == "active"
    assert theme.activated_at is not None
    assert theme.lifecycle_state_metadata["promotion_count"] == 1
    assert theme.lifecycle_state_metadata["continuity_id"] == "technical:ai_infrastructure"

    transitions = db_session.query(ThemeLifecycleTransition).filter(
        ThemeLifecycleTransition.theme_cluster_id == theme.id
    ).all()
    assert len(transitions) == 1
    assert transitions[0].from_state == "candidate"
    assert transitions[0].to_state == "active"

    alerts = db_session.query(ThemeAlert).filter(
        ThemeAlert.theme_cluster_id == theme.id,
        ThemeAlert.alert_type == "lifecycle_active",
    ).all()
    assert len(alerts) == 1
    assert alerts[0].metrics["reason"] == "candidate_promotion_thresholds_met"
    assert "transition_history_path" in alerts[0].metrics
    assert "runbook_url" in alerts[0].metrics


def test_candidate_promotion_uses_grouped_evidence_on_representative(db_session):
    now = datetime(2026, 2, 24, 15, 30, 0)
    source_a = _make_source(db_session, name="Grouped Alpha", source_type="news")
    source_b = _make_source(db_session, name="Grouped Bravo", source_type="substack")
    member = _make_theme(
        db_session,
        name="CPO",
        canonical_key="cpo",
        state="candidate",
        now=now,
    )
    representative = _make_theme(
        db_session,
        name="Co-Packaged Optics",
        canonical_key="co_packaged_optics",
        state="candidate",
        now=now,
    )
    service = ThemeDiscoveryService(db_session, pipeline="technical")
    assert service.groups.representative(member.id) == member.id
    for source, days_ago, suffix in [
        (source_a, 1, "1"),
        (source_b, 2, "2"),
        (source_a, 3, "3"),
        (source_b, 5, "4"),
    ]:
        _add_mention(
            db_session,
            theme=member,
            source=source,
            now=now,
            days_ago=days_ago,
            confidence=0.9,
            external_suffix=suffix,
        )
    ThemeEquivalenceService(db_session).apply(
        member.id,
        representative.id,
        actor="reviewer",
        reason="Equivalent exposure",
        key="lifecycle-promotion-group",
    )
    db_session.commit()

    result = service.promote_candidate_themes(now=now)

    db_session.refresh(member)
    db_session.refresh(representative)
    assert result["scanned"] == 1
    assert result["promoted"] == 1
    assert representative.lifecycle_state == "active"
    assert member.lifecycle_state == "candidate"
    transitions = db_session.query(ThemeLifecycleTransition).all()
    assert [transition.theme_cluster_id for transition in transitions] == [
        representative.id
    ]


def test_candidate_promotion_honors_current_grouped_social_lifecycle(db_session):
    now = datetime(2026, 2, 24, 15, 40, 0)
    member = _make_theme(
        db_session,
        name="Social CPO",
        canonical_key="social_cpo",
        state="candidate",
        now=now,
    )
    representative = _make_theme(
        db_session,
        name="Social Co-Packaged Optics",
        canonical_key="social_co_packaged_optics",
        state="candidate",
        now=now,
    )
    ThemeEquivalenceService(db_session).apply(
        member.id,
        representative.id,
        actor="reviewer",
        reason="Equivalent exposure",
        key="social-lifecycle-promotion-group",
    )
    member.lifecycle_state = "active"
    member.lifecycle_state_metadata = {
        "social_policy_version": "social-theme-v1",
        "social_valid_until": (
            now.replace(tzinfo=timezone.utc) + timedelta(days=1)
        ).isoformat(),
    }
    db_session.commit()

    result = ThemeDiscoveryService(
        db_session, pipeline="technical"
    ).promote_candidate_themes(now=now)

    db_session.refresh(representative)
    assert result["scanned"] == 1
    assert result["promoted"] == 1
    assert representative.lifecycle_state == "active"
    transition = db_session.query(ThemeLifecycleTransition).one()
    assert transition.theme_cluster_id == representative.id
    assert transition.reason == "grouped_social_lifecycle_evidence"


def test_dormancy_policy_uses_grouped_evidence_on_representative(db_session):
    now = datetime(2026, 2, 24, 15, 45, 0)
    source = _make_source(db_session, name="Grouped Current", source_type="news")
    member = _make_theme(
        db_session,
        name="Bitcoin Miners",
        canonical_key="bitcoin_miners",
        state="active",
        now=now,
    )
    representative = _make_theme(
        db_session,
        name="Bitcoin Mining",
        canonical_key="bitcoin_mining",
        state="active",
        now=now,
    )
    service = ThemeDiscoveryService(db_session, pipeline="technical")
    assert service.groups.representative(member.id) == member.id
    _add_mention(
        db_session,
        theme=member,
        source=source,
        now=now,
        days_ago=1,
        confidence=0.9,
        external_suffix="current",
    )
    ThemeEquivalenceService(db_session).apply(
        member.id,
        representative.id,
        actor="reviewer",
        reason="Equivalent exposure",
        key="lifecycle-dormancy-group",
    )
    db_session.commit()

    result = service.apply_dormancy_and_reactivation_policies(now=now)

    db_session.refresh(member)
    db_session.refresh(representative)
    assert result["scanned"] == 1
    assert result["to_dormant"] == 0
    assert representative.lifecycle_state == "active"
    assert member.lifecycle_state == "active"
    assert db_session.query(ThemeLifecycleTransition).count() == 0


@pytest.mark.parametrize(
    "method_name",
    ["promote_candidate_themes", "apply_dormancy_and_reactivation_policies"],
)
def test_standalone_lifecycle_pass_holds_group_publication_scope(
    db_session, monkeypatch, method_name
):
    from app.services import theme_group_coordination

    events = []

    @contextmanager
    def tracked_scope(db):
        events.append(("enter", db))
        yield
        events.append(("exit", db))

    monkeypatch.setattr(theme_group_coordination, "publication_scope", tracked_scope)

    service = ThemeDiscoveryService(db_session, pipeline="technical")
    getattr(service, method_name)(now=datetime(2026, 2, 24, 15, 47, 0))

    assert events == [("enter", db_session), ("exit", db_session)]


def test_dormancy_policy_honors_current_grouped_social_lifecycle(db_session):
    now = datetime(2026, 2, 24, 15, 50, 0)
    member = _make_theme(
        db_session,
        name="Social Bitcoin Miners",
        canonical_key="social_bitcoin_miners",
        state="active",
        now=now,
    )
    representative = _make_theme(
        db_session,
        name="Social Bitcoin Mining",
        canonical_key="social_bitcoin_mining",
        state="active",
        now=now,
    )
    member.lifecycle_state_metadata = {
        "social_policy_version": "social-theme-v1",
        "social_valid_until": (
            now.replace(tzinfo=timezone.utc) + timedelta(days=1)
        ).isoformat(),
    }
    ThemeEquivalenceService(db_session).apply(
        member.id,
        representative.id,
        actor="reviewer",
        reason="Equivalent exposure",
        key="social-lifecycle-dormancy-group",
    )
    db_session.commit()

    result = ThemeDiscoveryService(
        db_session, pipeline="technical"
    ).apply_dormancy_and_reactivation_policies(now=now)

    db_session.refresh(representative)
    assert result["scanned"] == 1
    assert result["to_dormant"] == 0
    assert representative.lifecycle_state == "active"
    assert db_session.query(ThemeLifecycleTransition).count() == 0


def test_dormant_representative_reactivates_from_grouped_social_lifecycle(
    db_session,
):
    now = datetime(2026, 2, 24, 15, 55, 0)
    member = _make_theme(
        db_session,
        name="Social CPO Member",
        canonical_key="social_cpo_member",
        state="dormant",
        now=now,
    )
    representative = _make_theme(
        db_session,
        name="Social CPO Representative",
        canonical_key="social_cpo_representative",
        state="dormant",
        now=now,
    )
    ThemeEquivalenceService(db_session).apply(
        member.id,
        representative.id,
        actor="reviewer",
        reason="Equivalent exposure",
        key="social-lifecycle-reactivation-group",
    )
    member.lifecycle_state = "active"
    member.lifecycle_state_metadata = {
        "social_policy_version": "social-theme-v1",
        "social_valid_until": (
            now.replace(tzinfo=timezone.utc) + timedelta(days=1)
        ).isoformat(),
    }
    db_session.commit()

    result = ThemeDiscoveryService(
        db_session, pipeline="technical"
    ).apply_dormancy_and_reactivation_policies(now=now)

    db_session.refresh(representative)
    assert result["scanned"] == 1
    assert result["to_reactivated"] == 1
    assert result["unchanged"] == 0
    assert representative.lifecycle_state == "reactivated"
    transition = db_session.query(ThemeLifecycleTransition).one()
    assert transition.theme_cluster_id == representative.id
    assert transition.reason == "grouped_social_lifecycle_evidence"


def test_dormancy_and_reactivation_policies_increment_counters(db_session):
    now = datetime(2026, 2, 24, 16, 0, 0)
    source_a = _make_source(db_session, name="Gamma Wire", source_type="news")
    source_b = _make_source(db_session, name="Delta Feed", source_type="substack")
    theme = _make_theme(
        db_session,
        name="Grid Modernization",
        canonical_key="grid_modernization",
        state="active",
        now=now,
    )
    _add_mention(db_session, theme=theme, source=source_a, now=now, days_ago=40, confidence=0.87, external_suffix="old")
    db_session.commit()

    service = ThemeDiscoveryService(db_session, pipeline="technical")
    dormancy_result = service.apply_dormancy_and_reactivation_policies(now=now)

    db_session.refresh(theme)
    assert dormancy_result["to_dormant"] == 1
    assert theme.lifecycle_state == "dormant"
    assert theme.lifecycle_state_metadata["dormancy_count"] == 1

    _add_mention(db_session, theme=theme, source=source_a, now=now, days_ago=1, confidence=0.90, external_suffix="new1")
    _add_mention(db_session, theme=theme, source=source_b, now=now, days_ago=2, confidence=0.86, external_suffix="new2")
    db_session.commit()

    reactivation_result = service.apply_dormancy_and_reactivation_policies(now=now + timedelta(minutes=1))

    db_session.refresh(theme)
    assert reactivation_result["to_reactivated"] == 1
    assert theme.lifecycle_state == "reactivated"
    assert theme.lifecycle_state_metadata["reactivation_count"] == 1
    assert theme.lifecycle_state_metadata["continuity_id"] == "technical:grid_modernization"

    alerts = db_session.query(ThemeAlert).filter(
        ThemeAlert.theme_cluster_id == theme.id,
        ThemeAlert.alert_type.in_(["lifecycle_dormant", "lifecycle_reactivated"]),
    ).all()
    alert_types = {a.alert_type for a in alerts}
    assert "lifecycle_dormant" in alert_types
    assert "lifecycle_reactivated" in alert_types


def test_calculate_mention_metrics_batch_matches_single_theme_metrics(db_session):
    now = datetime(2026, 2, 24, 16, 30, 0)
    source = _make_source(db_session, name="Metric Wire", source_type="news")
    theme = _make_theme(
        db_session,
        name="Grid Demand",
        canonical_key="grid_demand",
        state="active",
        now=now,
    )
    _add_mention(db_session, theme=theme, source=source, now=now, days_ago=1, confidence=0.9, external_suffix="1")
    _add_mention(db_session, theme=theme, source=source, now=now, days_ago=3, confidence=0.8, external_suffix="2")
    _add_mention(db_session, theme=theme, source=source, now=now, days_ago=20, confidence=0.7, external_suffix="3")
    db_session.commit()

    service = ThemeDiscoveryService(db_session, pipeline="technical")
    batch_metrics = service._calculate_mention_metrics_batch([theme.id], as_of_date=now)
    single_metrics = service.calculate_mention_metrics(theme.id, as_of_date=now)

    assert batch_metrics[theme.id] == single_metrics


def test_calculate_price_metrics_tolerates_sparse_prices_without_futurewarning(db_session):
    now = datetime(2026, 2, 24, 16, 30, 0)
    theme = _make_theme(
        db_session,
        name="Sparse Grid Demand",
        canonical_key="sparse_grid_demand",
        state="active",
        now=now,
    )
    db_session.add(
        ThemeConstituent(
            theme_cluster_id=theme.id,
            symbol="AAPL",
            source="manual",
            confidence=1.0,
            is_active=True,
        )
    )
    prices = [100.0, None, 101.0, 103.0, 104.0, 105.0]
    spy_prices = [400.0, None, 401.0, 402.0, 403.0, 404.0]
    for index, close in enumerate(prices):
        _add_stock_price(
            db_session,
            symbol="AAPL",
            trade_date=now - timedelta(days=5 - index),
            close=close,
        )
    for index, close in enumerate(spy_prices):
        _add_stock_price(
            db_session,
            symbol="SPY",
            trade_date=now - timedelta(days=5 - index),
            close=close,
        )
    db_session.commit()

    service = ThemeDiscoveryService(db_session, pipeline="technical")

    with warnings.catch_warnings():
        warnings.simplefilter("error", FutureWarning)
        metrics = service.calculate_price_metrics(theme.id, as_of_date=now)

    assert metrics["num_constituents"] == 1
    assert metrics["basket_rs_vs_spy"] >= 0


def _reference_price_metrics(service, theme_cluster_id, as_of_date):
    """calculate_price_metrics as it was before #419 step 2, kept verbatim as the oracle."""
    from collections import defaultdict

    import numpy as np
    import pandas as pd

    from app.services.theme_discovery_service import (
        compound_theme_returns,
        theme_relative_return_score,
    )

    constituents = service.db.query(ThemeConstituent).filter(
        ThemeConstituent.theme_cluster_id.in_(service.groups.members(theme_cluster_id)),
        ThemeConstituent.is_active == True,  # noqa: E712
    ).all()
    if not constituents:
        return service._empty_price_metrics()
    symbols = sorted({c.symbol for c in constituents})
    date_lookback = as_of_date - timedelta(days=260)
    prices_query = service.db.query(StockPrice).filter(
        StockPrice.symbol.in_(symbols),
        StockPrice.date >= date_lookback.date(),
        StockPrice.date <= as_of_date.date(),
    ).all()
    if not prices_query:
        return service._empty_price_metrics()
    price_data = defaultdict(list)
    for p in prices_query:
        price_data[p.symbol].append({"date": p.date, "close": p.close})
    returns_data, current_prices, ma_50, ma_200 = {}, {}, {}, {}
    for symbol, prices in price_data.items():
        if len(prices) < 5:
            continue
        df = pd.DataFrame(prices).sort_values("date")
        df["return"] = df["close"].pct_change(fill_method=None)
        current_prices[symbol] = df.iloc[-1]["close"]
        if len(df) >= 50:
            ma_50[symbol] = df["close"].tail(50).mean()
        if len(df) >= 200:
            ma_200[symbol] = df["close"].tail(200).mean()
        returns_data[symbol] = df.set_index("date")["return"]
    if not returns_data:
        return service._empty_price_metrics()
    returns_df = pd.DataFrame(returns_data)
    basket_returns = returns_df.mean(axis=1)
    spy_returns = service._load_spy_returns(as_of_date)

    def _compound_return(series, periods):
        value = compound_theme_returns(series.tail(periods).dropna(), periods)
        return value if value is not None else 0

    basket_return_1d = basket_returns.iloc[-1] if len(basket_returns) > 0 else 0
    basket_return_1w = _compound_return(basket_returns, 5)
    basket_return_1m = _compound_return(basket_returns, 21)
    basket_rs_vs_spy = theme_relative_return_score(basket_return_1m, _compound_return(spy_returns, 21))
    num_above_50ma = sum(1 for s, p in current_prices.items() if s in ma_50 and p > ma_50[s])
    num_above_200ma = sum(1 for s, p in current_prices.items() if s in ma_200 and p > ma_200[s])
    pct_above_50ma = num_above_50ma / len(current_prices) * 100 if current_prices else 0
    pct_above_200ma = num_above_200ma / len(current_prices) * 100 if current_prices else 0
    weekly_returns = (1 + returns_df.tail(5).fillna(0)).prod() - 1
    pct_positive_1w = (weekly_returns > 0).sum() / len(weekly_returns) * 100 if len(weekly_returns) > 0 else 0
    if len(returns_df.columns) >= 2:
        corr_matrix = returns_df.tail(21).corr()
        mask = np.triu(np.ones_like(corr_matrix, dtype=bool), k=1)
        correlations = corr_matrix.where(mask).stack().values
        avg_correlation = np.nanmean(correlations) if len(correlations) > 0 else 0
        correlation_tightness = np.nanstd(correlations) if len(correlations) > 0 else 0
    else:
        avg_correlation = 0
        correlation_tightness = 0
    result = service._empty_price_metrics()
    result.update({
        "basket_return_1d": round(basket_return_1d * 100, 2),
        "basket_return_1w": round(basket_return_1w * 100, 2),
        "basket_return_1m": round(basket_return_1m * 100, 2),
        "basket_rs_vs_spy": round(basket_rs_vs_spy, 1),
        "pct_above_50ma": round(pct_above_50ma, 1),
        "pct_above_200ma": round(pct_above_200ma, 1),
        "pct_positive_1w": round(pct_positive_1w, 1),
        "avg_internal_correlation": round(avg_correlation, 3),
        "correlation_tightness": round(correlation_tightness, 3),
        "num_constituents": len(symbols),
    })
    return result


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_calculate_price_metrics_matches_the_previous_algorithm(db_session, seed):
    """#419: same metrics as before the change, on histories with gaps, missing
    closes, and lengths either side of the 5/50/200-row thresholds."""
    import random

    rng = random.Random(seed)
    now = datetime(2026, 2, 24, 16, 30, 0)
    days = [now - timedelta(days=offset) for offset in range(259, -1, -1)]
    lengths = {"LONG": 240, "MID": 120, "EDGE": 50, "SHORT": 30, "TINY": 4, "GAPPY": 220}
    for symbol, length in lengths.items():
        for day in days[-length:]:
            if symbol == "GAPPY" and rng.random() < 0.15:
                continue  # missing session
            close = None if rng.random() < 0.03 else 50 + rng.random() * 50
            _add_stock_price(db_session, symbol=symbol, trade_date=day, close=close)
    for day in days:
        _add_stock_price(db_session, symbol="SPY", trade_date=day, close=400 + rng.random() * 20)

    baskets = {
        "broad": list(lengths),
        "short_only": ["TINY", "SHORT"],
        "single": ["LONG"],
        "tiny_only": ["TINY"],
        "unpriced": ["NOPRICE"],
    }
    themes = {}
    for name, members in baskets.items():
        theme = _make_theme(db_session, name=name, canonical_key=name, state="active", now=now)
        for symbol in members:
            db_session.add(ThemeConstituent(
                theme_cluster_id=theme.id, symbol=symbol, source="manual", confidence=1.0, is_active=True,
            ))
        themes[name] = theme
    db_session.commit()

    service = ThemeDiscoveryService(db_session, pipeline="technical")

    for name, theme in themes.items():
        assert service.calculate_price_metrics(theme.id, as_of_date=now) == _reference_price_metrics(
            service, theme.id, now
        ), name


def test_relationship_inference_writes_merge_and_overlap_edges(db_session):
    now = datetime.utcnow()
    theme_a = _make_theme(
        db_session,
        name="AI Chips",
        canonical_key="ai_chips",
        state="active",
        now=now,
    )
    theme_b = _make_theme(
        db_session,
        name="AI Infrastructure",
        canonical_key="ai_infrastructure",
        state="active",
        now=now,
    )
    theme_c = _make_theme(
        db_session,
        name="Defense Primes",
        canonical_key="defense_primes",
        state="active",
        now=now,
    )
    db_session.add_all(
        [
            ThemeConstituent(theme_cluster_id=theme_a.id, symbol="NVDA", is_active=True),
            ThemeConstituent(theme_cluster_id=theme_a.id, symbol="AVGO", is_active=True),
            ThemeConstituent(theme_cluster_id=theme_a.id, symbol="AMD", is_active=True),
            ThemeConstituent(theme_cluster_id=theme_b.id, symbol="NVDA", is_active=True),
            ThemeConstituent(theme_cluster_id=theme_b.id, symbol="AVGO", is_active=True),
            ThemeConstituent(theme_cluster_id=theme_b.id, symbol="AMD", is_active=True),
            ThemeConstituent(theme_cluster_id=theme_b.id, symbol="SMCI", is_active=True),
            ThemeConstituent(theme_cluster_id=theme_c.id, symbol="LMT", is_active=True),
            ThemeConstituent(theme_cluster_id=theme_c.id, symbol="NOC", is_active=True),
        ]
    )
    db_session.add(
        ThemeMergeSuggestion(
            source_cluster_id=theme_b.id,
            target_cluster_id=theme_c.id,
            embedding_similarity=0.73,
            llm_confidence=0.91,
            llm_relationship="distinct",
            llm_reasoning="Different sectors with little overlap.",
            status="rejected",
        )
    )
    db_session.commit()

    service = ThemeDiscoveryService(db_session, pipeline="technical")
    result = service.infer_theme_relationships(max_merge_suggestions=50)

    assert result["merge_edges_written"] >= 1
    assert result["rule_edges_written"] >= 1

    edges = db_session.query(ThemeRelationship).all()
    edge_keys = {(edge.source_cluster_id, edge.target_cluster_id, edge.relationship_type) for edge in edges}
    assert (theme_a.id, theme_b.id, "subset") in edge_keys


def test_lifecycle_thresholds_apply_admin_overrides_from_settings(db_session):
    db_session.add(
        AppSetting(
            key="theme_policy_overrides",
            value='{"technical":{"lifecycle":{"promotion_min_mentions_7d":9,"dormancy_inactivity_days":45}}}',
            category="theme",
            description="test overrides",
        )
    )
    db_session.commit()

    service = ThemeDiscoveryService(db_session, pipeline="technical")
    thresholds = service._lifecycle_thresholds()

    assert thresholds["promotion_min_mentions_7d"] == 9
    assert thresholds["dormancy_inactivity_days"] == 45


def test_relationship_inference_corrects_subset_direction_from_merge_suggestion(db_session):
    now = datetime.utcnow()
    subset_theme = _make_theme(
        db_session,
        name="AI Chips",
        canonical_key="ai_chips",
        state="active",
        now=now,
    )
    superset_theme = _make_theme(
        db_session,
        name="AI Infrastructure",
        canonical_key="ai_infrastructure",
        state="active",
        now=now,
    )
    db_session.add_all(
        [
            ThemeConstituent(theme_cluster_id=subset_theme.id, symbol="NVDA", is_active=True),
            ThemeConstituent(theme_cluster_id=subset_theme.id, symbol="AMD", is_active=True),
            ThemeConstituent(theme_cluster_id=superset_theme.id, symbol="NVDA", is_active=True),
            ThemeConstituent(theme_cluster_id=superset_theme.id, symbol="AMD", is_active=True),
            ThemeConstituent(theme_cluster_id=superset_theme.id, symbol="AVGO", is_active=True),
        ]
    )
    # Intentionally reversed direction in suggestion payload.
    db_session.add(
        ThemeMergeSuggestion(
            source_cluster_id=superset_theme.id,
            target_cluster_id=subset_theme.id,
            embedding_similarity=0.90,
            llm_confidence=0.94,
            llm_relationship="subset",
            llm_reasoning="AI Chips is a narrower part of AI Infrastructure.",
            status="pending",
        )
    )
    db_session.commit()

    service = ThemeDiscoveryService(db_session, pipeline="technical")
    result = service.infer_theme_relationships(max_merge_suggestions=20)
    assert result["merge_edges_written"] >= 1

    edge = db_session.query(ThemeRelationship).filter(
        ThemeRelationship.relationship_type == "subset",
        ThemeRelationship.pipeline == "technical",
    ).one()
    assert edge.source_cluster_id == subset_theme.id
    assert edge.target_cluster_id == superset_theme.id


def test_update_all_theme_metrics_applies_lifecycle_rank_weighting(db_session):
    now = datetime(2026, 2, 24, 18, 0, 0)
    candidate = _make_theme(
        db_session,
        name="Candidate Surge",
        canonical_key="candidate_surge",
        state="candidate",
        now=now,
    )
    active = _make_theme(
        db_session,
        name="Active Core",
        canonical_key="active_core",
        state="active",
        now=now,
    )
    db_session.commit()

    scores = {
        candidate.id: 90.0,
        active.id: 80.0,
    }

    service = ThemeDiscoveryService(db_session, pipeline="technical")

    def _stub_upsert_theme_metrics(*, cluster: ThemeCluster, as_of_date: datetime, mention_metrics: dict, auto_commit: bool) -> ThemeMetrics:
        _ = mention_metrics
        _ = auto_commit
        date_value = (as_of_date or now).date()
        metrics = db_session.query(ThemeMetrics).filter(
            ThemeMetrics.theme_cluster_id == cluster.id,
            ThemeMetrics.date == date_value,
        ).first()
        if metrics is None:
            metrics = ThemeMetrics(
                theme_cluster_id=cluster.id,
                date=date_value,
                pipeline="technical",
            )
            db_session.add(metrics)
        metrics.momentum_score = scores[cluster.id]
        metrics.status = "trending"
        db_session.flush()
        return metrics

    service._upsert_theme_metrics = _stub_upsert_theme_metrics  # type: ignore[method-assign]
    service.promote_candidate_themes = lambda now=None, limit=None, auto_commit=True: {"promoted": 0, "pipeline": "technical", "auto_commit": auto_commit}  # type: ignore[method-assign]
    service.apply_dormancy_and_reactivation_policies = lambda now=None, limit=None, auto_commit=True: {"to_dormant": 0, "to_reactivated": 0, "pipeline": "technical", "auto_commit": auto_commit}  # type: ignore[method-assign]
    result = service.update_all_theme_metrics(as_of_date=now)

    assert result["themes_updated"] == 2
    assert result["rankings"][0]["theme"] == "Active Core"
    assert result["rankings"][0]["lifecycle_state"] == "active"
    assert result["rankings"][1]["theme"] == "Candidate Surge"
    assert result["rankings"][1]["lifecycle_state"] == "candidate"


def test_update_all_theme_metrics_invokes_existing_lifecycle_policies_once(db_session):
    now = datetime(2026, 2, 24, 18, 30, 0)
    candidate = _make_theme(
        db_session,
        name="Candidate Promotion",
        canonical_key="candidate_promotion",
        state="candidate",
        now=now,
    )
    db_session.commit()

    service = ThemeDiscoveryService(db_session, pipeline="technical")
    calls = {"promote": 0, "state": 0}

    def _stub_upsert_theme_metrics(*, cluster: ThemeCluster, as_of_date: datetime, mention_metrics: dict, auto_commit: bool) -> ThemeMetrics:
        _ = mention_metrics
        _ = auto_commit
        metrics = ThemeMetrics(
            theme_cluster_id=cluster.id,
            date=as_of_date.date(),
            pipeline="technical",
            momentum_score=70.0,
            status="trending",
        )
        db_session.merge(metrics)
        db_session.flush()
        return db_session.query(ThemeMetrics).filter(
            ThemeMetrics.theme_cluster_id == cluster.id,
            ThemeMetrics.date == as_of_date.date(),
        ).one()

    def _stub_promote(now=None, limit=None, auto_commit=True):
        _ = limit
        calls["promote"] += 1
        assert auto_commit is False
        candidate.lifecycle_state = "active"
        db_session.flush()
        return {"promoted": 1, "pipeline": "technical", "now": now.isoformat() if now else None}

    def _stub_state(now=None, limit=None, auto_commit=True):
        _ = limit
        calls["state"] += 1
        assert auto_commit is False
        return {"to_dormant": 0, "to_reactivated": 0, "pipeline": "technical", "now": now.isoformat() if now else None}

    service._upsert_theme_metrics = _stub_upsert_theme_metrics  # type: ignore[method-assign]
    service.promote_candidate_themes = _stub_promote  # type: ignore[method-assign]
    service.apply_dormancy_and_reactivation_policies = _stub_state  # type: ignore[method-assign]

    result = service.update_all_theme_metrics(as_of_date=now)

    assert calls == {"promote": 1, "state": 1}
    assert result["lifecycle"]["candidate_promotion"]["promoted"] == 1
    assert result["rankings"][0]["lifecycle_state"] == "active"


def test_update_all_theme_metrics_uses_single_commit_for_lifecycle_refresh(db_session, monkeypatch):
    now = datetime(2026, 2, 24, 18, 45, 0)
    source_a = _make_source(db_session, name="Alpha Desk", source_type="news")
    source_b = _make_source(db_session, name="Bravo Research", source_type="substack")
    candidate = _make_theme(
        db_session,
        name="Atomic Promotion",
        canonical_key="atomic_promotion",
        state="candidate",
        now=now,
    )
    _add_mention(db_session, theme=candidate, source=source_a, now=now, days_ago=1, confidence=0.92, external_suffix="1")
    _add_mention(db_session, theme=candidate, source=source_b, now=now, days_ago=2, confidence=0.88, external_suffix="2")
    _add_mention(db_session, theme=candidate, source=source_a, now=now, days_ago=3, confidence=0.89, external_suffix="3")
    _add_mention(db_session, theme=candidate, source=source_b, now=now, days_ago=5, confidence=0.91, external_suffix="4")
    db_session.commit()

    service = ThemeDiscoveryService(db_session, pipeline="technical")

    def _stub_upsert_theme_metrics(*, cluster: ThemeCluster, as_of_date: datetime, mention_metrics: dict, auto_commit: bool) -> ThemeMetrics:
        _ = mention_metrics
        _ = auto_commit
        metrics = db_session.query(ThemeMetrics).filter(
            ThemeMetrics.theme_cluster_id == cluster.id,
            ThemeMetrics.date == as_of_date.date(),
        ).first()
        if metrics is None:
            metrics = ThemeMetrics(
                theme_cluster_id=cluster.id,
                date=as_of_date.date(),
                pipeline="technical",
            )
            db_session.add(metrics)
        metrics.momentum_score = 70.0
        metrics.status = "trending"
        db_session.flush()
        return metrics

    original_commit = db_session.commit
    commit_calls = {"count": 0}

    def _counting_commit():
        commit_calls["count"] += 1
        return original_commit()

    monkeypatch.setattr(db_session, "commit", _counting_commit)
    service._upsert_theme_metrics = _stub_upsert_theme_metrics  # type: ignore[method-assign]

    result = service.update_all_theme_metrics(as_of_date=now)

    db_session.refresh(candidate)
    assert commit_calls["count"] == 1
    assert result["lifecycle"]["candidate_promotion"]["promoted"] == 1
    assert candidate.lifecycle_state == "active"
    assert result["rankings"][0]["rank"] == 1


def test_get_theme_rankings_filters_by_lifecycle_state(db_session):
    now = datetime(2026, 2, 24, 19, 0, 0)
    candidate = _make_theme(
        db_session,
        name="Candidate Grid",
        canonical_key="candidate_grid",
        state="candidate",
        now=now,
    )
    active = _make_theme(
        db_session,
        name="Active Grid",
        canonical_key="active_grid",
        state="active",
        now=now,
    )
    db_session.add_all(
        [
            ThemeMetrics(
                theme_cluster_id=candidate.id,
                date=now.date(),
                pipeline="technical",
                momentum_score=74.0,
                rank=2,
                status="emerging",
                mentions_7d=5,
                mention_velocity=1.6,
            ),
            ThemeMetrics(
                theme_cluster_id=active.id,
                date=now.date(),
                pipeline="technical",
                momentum_score=79.0,
                rank=1,
                status="trending",
                mentions_7d=7,
                mention_velocity=1.8,
            ),
        ]
    )
    db_session.commit()

    service = ThemeDiscoveryService(db_session, pipeline="technical")
    rankings, total = service.get_theme_rankings(lifecycle_states_filter=["candidate"])

    assert total == 1
    assert len(rankings) == 1
    assert rankings[0]["theme"] == "Candidate Grid"
    assert rankings[0]["lifecycle_state"] == "candidate"


def test_discover_emerging_themes_suppresses_noisy_candidates_by_lifecycle_gate(db_session):
    now = datetime.utcnow()
    source_news = _make_source(db_session, name="News Wire", source_type="news")
    source_substack = _make_source(db_session, name="Research Letter", source_type="substack")

    noisy_candidate = _make_theme(
        db_session,
        name="Noisy Candidate",
        canonical_key="noisy_candidate",
        state="candidate",
        now=now,
    )
    valid_active = _make_theme(
        db_session,
        name="Valid Active",
        canonical_key="valid_active",
        state="active",
        now=now,
    )
    noisy_candidate.first_seen_at = now - timedelta(days=2)
    valid_active.first_seen_at = now - timedelta(days=2)

    _add_mention(
        db_session,
        theme=noisy_candidate,
        source=source_news,
        now=now,
        days_ago=1,
        confidence=0.85,
        external_suffix="noisy1",
    )
    _add_mention(
        db_session,
        theme=noisy_candidate,
        source=source_news,
        now=now,
        days_ago=1,
        confidence=0.84,
        external_suffix="noisy2",
    )
    _add_mention(
        db_session,
        theme=noisy_candidate,
        source=source_news,
        now=now,
        days_ago=2,
        confidence=0.83,
        external_suffix="noisy3",
    )

    _add_mention(
        db_session,
        theme=valid_active,
        source=source_news,
        now=now,
        days_ago=1,
        confidence=0.86,
        external_suffix="active1",
    )
    _add_mention(
        db_session,
        theme=valid_active,
        source=source_substack,
        now=now,
        days_ago=2,
        confidence=0.88,
        external_suffix="active2",
    )
    _add_mention(
        db_session,
        theme=valid_active,
        source=source_substack,
        now=now,
        days_ago=3,
        confidence=0.87,
        external_suffix="active3",
    )
    db_session.commit()

    service = ThemeDiscoveryService(db_session, pipeline="technical")
    emerging = service.discover_emerging_themes(min_velocity=1.0, min_mentions=3)
    names = {entry["theme"] for entry in emerging}

    assert "Valid Active" in names
    assert "Noisy Candidate" not in names


def test_get_theme_rankings_invalid_lifecycle_filter_returns_empty(db_session):
    now = datetime(2026, 2, 24, 20, 0, 0)
    active = _make_theme(
        db_session,
        name="Active Compute",
        canonical_key="active_compute",
        state="active",
        now=now,
    )
    db_session.add(
        ThemeMetrics(
            theme_cluster_id=active.id,
            date=now.date(),
            pipeline="technical",
            momentum_score=82.0,
            rank=1,
            status="trending",
            mentions_7d=8,
            mention_velocity=1.7,
        )
    )
    db_session.commit()

    service = ThemeDiscoveryService(db_session, pipeline="technical")
    rankings, total = service.get_theme_rankings(lifecycle_states_filter=["not_a_state"])

    assert total == 0
    assert rankings == []


def test_discover_emerging_themes_dormant_requires_confidence_threshold(db_session):
    now = datetime.utcnow()
    source_news = _make_source(db_session, name="Dormancy Wire", source_type="news")
    source_substack = _make_source(db_session, name="Dormancy Letter", source_type="substack")

    low_quality_dormant = _make_theme(
        db_session,
        name="Low Quality Dormant",
        canonical_key="low_quality_dormant",
        state="dormant",
        now=now,
    )
    high_quality_dormant = _make_theme(
        db_session,
        name="High Quality Dormant",
        canonical_key="high_quality_dormant",
        state="dormant",
        now=now,
    )
    low_quality_dormant.first_seen_at = now - timedelta(days=2)
    high_quality_dormant.first_seen_at = now - timedelta(days=2)

    _add_mention(
        db_session,
        theme=low_quality_dormant,
        source=source_news,
        now=now,
        days_ago=1,
        confidence=0.20,
        external_suffix="low1",
    )
    _add_mention(
        db_session,
        theme=low_quality_dormant,
        source=source_substack,
        now=now,
        days_ago=2,
        confidence=0.18,
        external_suffix="low2",
    )

    _add_mention(
        db_session,
        theme=high_quality_dormant,
        source=source_news,
        now=now,
        days_ago=1,
        confidence=0.87,
        external_suffix="high1",
    )
    _add_mention(
        db_session,
        theme=high_quality_dormant,
        source=source_substack,
        now=now,
        days_ago=2,
        confidence=0.86,
        external_suffix="high2",
    )
    db_session.commit()

    service = ThemeDiscoveryService(db_session, pipeline="technical")
    emerging = service.discover_emerging_themes(min_velocity=1.0, min_mentions=2)
    names = {entry["theme"] for entry in emerging}

    assert "High Quality Dormant" in names
    assert "Low Quality Dormant" not in names


def test_get_lifecycle_transition_history_returns_context_rows(db_session):
    now = datetime(2026, 2, 24, 21, 0, 0)
    source = _make_source(db_session, name="History Wire", source_type="news")
    source_two = _make_source(db_session, name="History Letter", source_type="substack")
    theme = _make_theme(
        db_session,
        name="History Theme",
        canonical_key="history_theme",
        state="candidate",
        now=now,
    )
    _add_mention(db_session, theme=theme, source=source, now=now, days_ago=1, confidence=0.92, external_suffix="1")
    _add_mention(db_session, theme=theme, source=source_two, now=now, days_ago=2, confidence=0.91, external_suffix="2")
    _add_mention(db_session, theme=theme, source=source, now=now, days_ago=3, confidence=0.93, external_suffix="3")
    _add_mention(db_session, theme=theme, source=source_two, now=now, days_ago=4, confidence=0.90, external_suffix="4")
    db_session.commit()

    service = ThemeDiscoveryService(db_session, pipeline="technical")
    service.promote_candidate_themes(now=now)

    rows, total = service.get_lifecycle_transition_history(theme_cluster_id=theme.id, limit=10, offset=0)
    assert total == 1
    assert len(rows) == 1
    assert rows[0]["theme_cluster_id"] == theme.id
    assert rows[0]["from_state"] == "candidate"
    assert rows[0]["to_state"] == "active"
    assert rows[0]["reason"] == "candidate_promotion_thresholds_met"
    assert "transition_history_path" in rows[0]
    assert "runbook_url" in rows[0]


def test_update_all_theme_metrics_loads_spy_once_per_run(db_session):
    """#419: the SPY benchmark series is the same for every theme in a run; load it once."""
    from sqlalchemy import event

    now = datetime(2026, 2, 24, 18, 0, 0)
    themes = []
    for name, key, symbol in (("Grid Demand", "grid_demand", "AAPL"), ("Chip Supply", "chip_supply", "MSFT")):
        theme = _make_theme(db_session, name=name, canonical_key=key, state="active", now=now)
        db_session.add(ThemeConstituent(
            theme_cluster_id=theme.id, symbol=symbol, source="manual", confidence=1.0, is_active=True,
        ))
        themes.append(theme)
    # 30 sessions so the 21-period (1-month) RS vs SPY is actually computed.
    for index in range(30):
        day = now - timedelta(days=29 - index)
        # Small moves keep the RS score off its 0/100 clamps, so SPY changes show.
        _add_stock_price(db_session, symbol="AAPL", trade_date=day, close=100.0 + index * 0.1)
        _add_stock_price(db_session, symbol="MSFT", trade_date=day, close=200.0 - index * 0.1)
        _add_stock_price(db_session, symbol="SPY", trade_date=day, close=400.0 + index * 0.5)
    db_session.commit()

    service = ThemeDiscoveryService(db_session, pipeline="technical")
    service.promote_candidate_themes = lambda now=None, limit=None, auto_commit=True: {"promoted": 0}  # type: ignore[method-assign]
    service.apply_dormancy_and_reactivation_policies = lambda now=None, limit=None, auto_commit=True: {}  # type: ignore[method-assign]

    spy_queries = []

    def _count_spy(conn, cursor, statement, parameters, context, executemany):
        if "stock_prices" in statement and "SPY" in str(parameters):
            spy_queries.append(statement)

    engine = db_session.get_bind()
    event.listen(engine, "before_cursor_execute", _count_spy)
    try:
        result = service.update_all_theme_metrics(as_of_date=now)
    finally:
        event.remove(engine, "before_cursor_execute", _count_spy)

    assert result["themes_updated"] == 2
    assert len(spy_queries) == 1

    # Shared SPY series gives the same RS as a standalone per-theme computation.
    standalone = ThemeDiscoveryService(db_session, pipeline="technical")
    for theme in themes:
        stored = db_session.query(ThemeMetrics).filter(
            ThemeMetrics.theme_cluster_id == theme.id, ThemeMetrics.date == now.date(),
        ).one()
        expected = standalone.calculate_price_metrics(theme.id, as_of_date=now)
        assert stored.basket_rs_vs_spy == pytest.approx(expected["basket_rs_vs_spy"])

    # The run's SPY cache must not outlive the run: a later same-date call on
    # the same instance has to see SPY rows written after the run.
    latest_spy = db_session.query(StockPrice).filter(
        StockPrice.symbol == "SPY", StockPrice.date == now.date(),
    ).one()
    latest_spy.close = 380.0
    db_session.commit()
    reused = service.calculate_price_metrics(themes[0].id, as_of_date=now)
    fresh = ThemeDiscoveryService(db_session, pipeline="technical").calculate_price_metrics(
        themes[0].id, as_of_date=now,
    )
    assert reused["basket_rs_vs_spy"] == pytest.approx(fresh["basket_rs_vs_spy"])


def test_update_all_theme_metrics_looks_up_latest_scan_once_per_run(db_session):
    """#467: the latest completed scan is the same for every theme in a run; look it up once."""
    from app.models.scan_result import Scan, ScanResult
    from sqlalchemy import event

    now = datetime(2026, 2, 24, 18, 0, 0)

    def _add_scan(scan_id, completed_at, rows):
        db_session.add(Scan(
            scan_id=scan_id, status="completed", screener_types=["minervini"],
            started_at=completed_at - timedelta(minutes=5), completed_at=completed_at,
        ))
        for symbol, minervini, stage, rs in rows:
            db_session.add(ScanResult(
                scan_id=scan_id, symbol=symbol, minervini_score=minervini, stage=stage, rs_rating=rs,
            ))

    themes = []
    for name, key, symbol in (("Grid Demand", "grid_demand", "AAPL"), ("Chip Supply", "chip_supply", "MSFT")):
        theme = _make_theme(db_session, name=name, canonical_key=key, state="active", now=now)
        db_session.add(ThemeConstituent(
            theme_cluster_id=theme.id, symbol=symbol, source="manual", confidence=1.0, is_active=True,
        ))
        themes.append(theme)
    _add_scan("old-scan", now - timedelta(days=2), [("AAPL", 10.0, 1, 20.0), ("MSFT", 10.0, 1, 30.0)])
    _add_scan("new-scan", now - timedelta(days=1), [("AAPL", 80.0, 2, 90.0), ("MSFT", 50.0, 2, 60.0)])
    db_session.commit()

    service = ThemeDiscoveryService(db_session, pipeline="technical")
    service.promote_candidate_themes = lambda now=None, limit=None, auto_commit=True: {"promoted": 0}  # type: ignore[method-assign]
    service.apply_dormancy_and_reactivation_policies = lambda now=None, limit=None, auto_commit=True: {}  # type: ignore[method-assign]

    scan_queries = []

    def _count_scans(conn, cursor, statement, parameters, context, executemany):
        if "FROM scans" in statement:
            scan_queries.append(statement)

    engine = db_session.get_bind()
    event.listen(engine, "before_cursor_execute", _count_scans)
    try:
        result = service.update_all_theme_metrics(as_of_date=now)
    finally:
        event.remove(engine, "before_cursor_execute", _count_scans)

    assert result["themes_updated"] == 2
    assert len(scan_queries) == 1

    # Each theme still reads the newest scan's results.
    stored = {
        theme.id: db_session.query(ThemeMetrics).filter(
            ThemeMetrics.theme_cluster_id == theme.id, ThemeMetrics.date == now.date(),
        ).one()
        for theme in themes
    }
    assert (stored[themes[0].id].num_passing_minervini, stored[themes[0].id].avg_rs_rating) == (1, 90.0)
    assert (stored[themes[1].id].num_passing_minervini, stored[themes[1].id].avg_rs_rating) == (0, 60.0)
    assert all(row.num_stage_2 == 1 for row in stored.values())

    # The run's cache must not outlive the run: a reused instance sees a newer scan.
    _add_scan("newest-scan", now, [("AAPL", 10.0, 3, 5.0)])
    db_session.commit()
    assert service.calculate_screener_metrics(themes[0].id) == {
        "num_passing_minervini": 0, "num_stage_2": 0, "avg_rs_rating": 5.0,
    }


def test_screener_metrics_ignore_a_newer_watchlist_or_test_scan(db_session):
    """A custom (watchlist) or test scan finishing last must not feed theme metrics."""
    from app.models.scan_result import Scan, ScanResult

    now = datetime(2026, 2, 24, 18, 0, 0)
    theme = _make_theme(db_session, name="Grid Demand", canonical_key="grid_demand", state="active", now=now)
    db_session.add(ThemeConstituent(
        theme_cluster_id=theme.id, symbol="AAPL", source="manual", confidence=1.0, is_active=True,
    ))
    for scan_id, universe_type, market, completed_at, minervini, stage, rs in (
        ("old-us-scan", "market", "US", now - timedelta(hours=4), 10.0, 1, 10.0),
        ("us-scan", "market", "US", now - timedelta(hours=3), 80.0, 2, 90.0),
        ("watchlist-scan", "custom", None, now - timedelta(hours=2), 10.0, 1, 20.0),
        ("test-scan", "test", None, now - timedelta(hours=1), 10.0, 3, 30.0),
    ):
        db_session.add(Scan(
            scan_id=scan_id, status="completed", universe_type=universe_type, universe_market=market,
            started_at=completed_at - timedelta(minutes=5), completed_at=completed_at,
        ))
        db_session.add(ScanResult(
            scan_id=scan_id, symbol="AAPL", minervini_score=minervini, stage=stage, rs_rating=rs,
        ))
    db_session.commit()

    service = ThemeDiscoveryService(db_session, pipeline="technical")

    assert service.calculate_screener_metrics(theme.id) == {
        "num_passing_minervini": 1, "num_stage_2": 1, "avg_rs_rating": 90.0,
    }
