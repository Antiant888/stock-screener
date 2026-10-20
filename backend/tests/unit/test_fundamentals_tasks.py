from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from celery.exceptions import Retry, SoftTimeLimitExceeded


def _patch_serialized_lock(monkeypatch):
    import app.services.provider_snapshot_service as provider_snapshot_module

    fake_lock = MagicMock()
    fake_lock.acquire.return_value = (True, False)
    fake_lock.release.return_value = True
    monkeypatch.setattr(
        "app.wiring.bootstrap.get_data_fetch_lock",
        lambda: fake_lock,
    )
    fake_coordination = MagicMock()
    fake_coordination.acquire_market_workload.return_value = (True, False)
    fake_coordination.release_market_workload.return_value = True
    fake_coordination.acquire_external_fetch.return_value = (True, False)
    fake_coordination.release_external_fetch.return_value = True
    monkeypatch.setattr(
        "app.wiring.bootstrap.get_workload_coordination",
        lambda: fake_coordination,
    )
    monkeypatch.setattr(
        provider_snapshot_module.settings,
        "market_data_source_mode",
        "live_only",
    )


def test_refresh_all_fundamentals_retries_transient_outer_failures(monkeypatch):
    import app.tasks.fundamentals_tasks as module

    fake_db = MagicMock()
    fake_query = MagicMock()
    fake_query.filter.return_value.all.return_value = [
        SimpleNamespace(symbol="AAPL", market="US")
    ]
    # Unscoped runs add an enabled-markets filter (#414).
    fake_query.filter.return_value.filter.return_value.all.return_value = [
        SimpleNamespace(symbol="AAPL", market="US")
    ]
    fake_db.query.return_value = fake_query
    monkeypatch.setattr(module, "SessionLocal", lambda: fake_db)
    _patch_serialized_lock(monkeypatch)
    monkeypatch.setattr(module, "get_fundamentals_cache", lambda: (_ for _ in ()).throw(ConnectionError("provider down")))

    retry_calls = []

    def fake_retry(*args, **kwargs):
        retry_calls.append(kwargs)
        raise Retry("retry")

    monkeypatch.setattr(module.refresh_all_fundamentals, "retry", fake_retry)
    module.refresh_all_fundamentals.request.id = "task-123"
    module.refresh_all_fundamentals.request.retries = 0

    with pytest.raises(Retry):
        module.refresh_all_fundamentals.run()

    fake_db.rollback.assert_called_once()
    assert retry_calls[0]["max_retries"] == 2
    assert retry_calls[0]["countdown"] == 60
    assert module.refresh_all_fundamentals.soft_time_limit == 7200


def test_refresh_all_fundamentals_retry_survives_activity_publish_failure(monkeypatch):
    import app.tasks.fundamentals_tasks as module

    fake_db = MagicMock()
    fake_query = MagicMock()
    fake_query.filter.return_value.all.return_value = [
        SimpleNamespace(symbol="AAPL", market="US")
    ]
    # Unscoped runs add an enabled-markets filter (#414).
    fake_query.filter.return_value.filter.return_value.all.return_value = [
        SimpleNamespace(symbol="AAPL", market="US")
    ]
    fake_db.query.return_value = fake_query
    monkeypatch.setattr(module, "SessionLocal", lambda: fake_db)
    _patch_serialized_lock(monkeypatch)
    monkeypatch.setattr(module, "get_fundamentals_cache", lambda: (_ for _ in ()).throw(ConnectionError("provider down")))
    monkeypatch.setattr(
        module,
        "mark_market_activity_failed",
        MagicMock(side_effect=RuntimeError("activity store unavailable")),
    )

    retry_calls = []

    def fake_retry(*args, **kwargs):
        retry_calls.append(kwargs)
        raise Retry("retry")

    monkeypatch.setattr(module.refresh_all_fundamentals, "retry", fake_retry)
    module.refresh_all_fundamentals.request.id = "task-123"
    module.refresh_all_fundamentals.request.retries = 0

    with pytest.raises(Retry):
        module.refresh_all_fundamentals.run()

    assert retry_calls[0]["countdown"] == 60


def test_refresh_all_fundamentals_reraises_soft_time_limit(monkeypatch):
    import app.tasks.fundamentals_tasks as module

    fake_db = MagicMock()
    fake_query = MagicMock()
    fake_query.filter.return_value.all.return_value = [
        SimpleNamespace(symbol="AAPL", market="US")
    ]
    # Unscoped runs add an enabled-markets filter (#414).
    fake_query.filter.return_value.filter.return_value.all.return_value = [
        SimpleNamespace(symbol="AAPL", market="US")
    ]
    fake_db.query.return_value = fake_query
    monkeypatch.setattr(module, "SessionLocal", lambda: fake_db)
    _patch_serialized_lock(monkeypatch)
    monkeypatch.setattr(module, "get_fundamentals_cache", lambda: (_ for _ in ()).throw(SoftTimeLimitExceeded()))

    with pytest.raises(SoftTimeLimitExceeded):
        module.refresh_all_fundamentals.run()

    fake_db.rollback.assert_called_once()


def test_refresh_all_fundamentals_reraises_nested_soft_time_limit(monkeypatch):
    import app.tasks.fundamentals_tasks as module

    fake_db = MagicMock()
    fake_query = MagicMock()
    fake_query.filter.return_value.all.return_value = [
        SimpleNamespace(symbol="AAPL", market="US")
    ]
    # Unscoped runs add an enabled-markets filter (#414).
    fake_query.filter.return_value.filter.return_value.all.return_value = [
        SimpleNamespace(symbol="AAPL", market="US")
    ]
    fake_db.query.return_value = fake_query
    monkeypatch.setattr(module, "SessionLocal", lambda: fake_db)
    _patch_serialized_lock(monkeypatch)

    fake_cache = MagicMock()
    fake_cache.get_fundamentals.side_effect = SoftTimeLimitExceeded()
    monkeypatch.setattr(module, "get_fundamentals_cache", lambda: fake_cache)

    with pytest.raises(SoftTimeLimitExceeded):
        module.refresh_all_fundamentals.run()

    fake_db.rollback.assert_called_once()


def test_refresh_all_fundamentals_publishes_market_activity(monkeypatch):
    import app.tasks.fundamentals_tasks as module

    fake_db = MagicMock()
    fake_query = MagicMock()
    fake_query.filter.return_value.filter.return_value.all.return_value = [
        SimpleNamespace(symbol="AAPL", market="US")
    ]
    fake_query.filter.return_value.all.return_value = [
        SimpleNamespace(symbol="AAPL", market="US")
    ]
    # Unscoped runs add an enabled-markets filter (#414).
    fake_query.filter.return_value.filter.return_value.all.return_value = [
        SimpleNamespace(symbol="AAPL", market="US")
    ]
    fake_db.query.return_value = fake_query
    monkeypatch.setattr(module, "SessionLocal", lambda: fake_db)
    _patch_serialized_lock(monkeypatch)
    monkeypatch.setattr(
        module,
        "get_fundamentals_cache",
        lambda: SimpleNamespace(get_fundamentals=lambda *args, **kwargs: {"symbol": "AAPL"}),
    )
    monkeypatch.setattr(module, "get_ticker_validation_service", lambda: MagicMock())
    monkeypatch.setattr(
        module.calculate_eps_rating_percentiles,
        "delay",
        lambda: SimpleNamespace(id="eps-task-id"),
    )

    started = []
    completed = []
    monkeypatch.setattr(module, "mark_market_activity_started", lambda *args, **kwargs: started.append(kwargs))
    monkeypatch.setattr(module, "mark_market_activity_completed", lambda *args, **kwargs: completed.append(kwargs))

    result = module.refresh_all_fundamentals.run(market="US")

    assert result["updated"] == 1
    assert started[0]["stage_key"] == "fundamentals"
    assert started[0]["lifecycle"] == "weekly_refresh"
    assert completed[0]["stage_key"] == "fundamentals"
    assert completed[0]["market"] == "US"


def test_refresh_all_fundamentals_prefers_github_weekly_bundle(monkeypatch):
    import app.tasks.fundamentals_tasks as module

    fake_db = MagicMock()
    fake_query = MagicMock()
    fake_query.filter.return_value.all.return_value = [
        SimpleNamespace(symbol="AAPL", market="US")
    ]
    # Unscoped runs add an enabled-markets filter (#414).
    fake_query.filter.return_value.filter.return_value.all.return_value = [
        SimpleNamespace(symbol="AAPL", market="US")
    ]
    fake_db.query.return_value = fake_query
    monkeypatch.setattr(module, "SessionLocal", lambda: fake_db)
    _patch_serialized_lock(monkeypatch)
    monkeypatch.setattr(
        module,
        "get_provider_snapshot_service",
        lambda: SimpleNamespace(
            sync_weekly_reference_from_github=lambda db, market, hydrate_cache, hydrate_mode: {
                "status": "success",
                "source": "github",
                "market": market,
                "source_revision": "fundamentals_v1_us:20260418120000",
                "import": {"rows": 1, "hydrated_symbols": 1},
            }
        ),
    )
    monkeypatch.setattr(
        module,
        "get_fundamentals_cache",
        lambda: (_ for _ in ()).throw(AssertionError("legacy live fetch should not run")),
    )
    monkeypatch.setattr(
        module.calculate_eps_rating_percentiles,
        "delay",
        lambda: SimpleNamespace(id="eps-task-id"),
    )

    started = []
    completed = []
    monkeypatch.setattr(module, "mark_market_activity_started", lambda *args, **kwargs: started.append(kwargs))
    monkeypatch.setattr(module, "mark_market_activity_completed", lambda *args, **kwargs: completed.append(kwargs))

    result = module.refresh_all_fundamentals.run(market="US")

    assert result["status"] == "success"
    assert result["source"] == "github"
    assert result["eps_rating_task_id"] == "eps-task-id"
    assert started[0]["stage_key"] == "fundamentals"
    assert completed[0]["stage_key"] == "fundamentals"


def test_refresh_all_fundamentals_publishes_running_progress(monkeypatch):
    import app.tasks.fundamentals_tasks as module

    fake_db = MagicMock()
    stocks = [SimpleNamespace(symbol=f"SYM{i}", market="US") for i in range(30)]
    fake_query = MagicMock()
    fake_query.filter.return_value.filter.return_value.all.return_value = stocks
    fake_query.filter.return_value.all.return_value = stocks
    fake_db.query.return_value = fake_query
    monkeypatch.setattr(module, "SessionLocal", lambda: fake_db)
    _patch_serialized_lock(monkeypatch)
    monkeypatch.setattr(
        module,
        "get_fundamentals_cache",
        lambda: SimpleNamespace(get_fundamentals=lambda *args, **kwargs: {"symbol": kwargs.get("symbol", "SYM")}),
    )
    monkeypatch.setattr(module, "get_ticker_validation_service", lambda: MagicMock())
    monkeypatch.setattr(
        module.calculate_eps_rating_percentiles,
        "delay",
        lambda: SimpleNamespace(id="eps-task-id"),
    )

    progress_updates = []
    monkeypatch.setattr(module, "mark_market_activity_progress", lambda *args, **kwargs: progress_updates.append(kwargs))

    result = module.refresh_all_fundamentals.run(market="US")

    assert result["updated"] == 30
    assert progress_updates
    assert progress_updates[0]["market"] == "US"
    assert progress_updates[0]["stage_key"] == "fundamentals"
    assert all(update["total"] == 30 for update in progress_updates)
    assert any(update["current"] < update["total"] for update in progress_updates)
    assert any(update["percent"] > 0 for update in progress_updates)


def test_refresh_all_fundamentals_progress_counts_failed_iterations(monkeypatch):
    import app.tasks.fundamentals_tasks as module

    fake_db = MagicMock()
    stocks = [SimpleNamespace(symbol=f"SYM{i}", market="US") for i in range(30)]
    fake_query = MagicMock()
    fake_query.filter.return_value.filter.return_value.all.return_value = stocks
    fake_query.filter.return_value.all.return_value = stocks
    fake_db.query.return_value = fake_query
    monkeypatch.setattr(module, "SessionLocal", lambda: fake_db)
    _patch_serialized_lock(monkeypatch)

    fake_cache = MagicMock()
    fake_cache.get_fundamentals.side_effect = RuntimeError("provider unavailable")
    monkeypatch.setattr(module, "get_fundamentals_cache", lambda: fake_cache)

    validation_service = MagicMock()
    validation_service.classify_error.return_value = ("provider_error", "provider unavailable")
    monkeypatch.setattr(module, "get_ticker_validation_service", lambda: validation_service)
    monkeypatch.setattr(
        module.calculate_eps_rating_percentiles,
        "delay",
        lambda: SimpleNamespace(id="eps-task-id"),
    )

    progress_updates = []
    monkeypatch.setattr(module, "mark_market_activity_progress", lambda *args, **kwargs: progress_updates.append(kwargs))

    result = module.refresh_all_fundamentals.run(market="US")

    assert result["failed"] == 30
    assert progress_updates
    assert any(update["current"] < update["total"] for update in progress_updates)
    assert progress_updates[-1]["current"] == 30
    assert progress_updates[-1]["total"] == 30


# ── #414: fundamentals refreshes stay inside runtime-enabled markets ─────

_UNIVERSE = (("AAPL", "US"), ("0700.HK", "HK"), ("7203.T", "JP"))


def _universe_session_factory():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.database import Base
    from app.models.stock_universe import UNIVERSE_STATUS_ACTIVE, StockUniverse

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    db = factory()
    for symbol, market in _UNIVERSE:
        db.add(StockUniverse(
            symbol=symbol, market=market, exchange="X", is_active=True,
            status=UNIVERSE_STATUS_ACTIVE, status_reason="active",
        ))
    db.commit()
    db.close()
    return factory


def _runtime_markets(monkeypatch, *enabled, primary="US"):
    import app.services.runtime_preferences_service as prefs

    monkeypatch.setattr(
        prefs,
        "runtime_preferences_now",
        lambda: SimpleNamespace(primary_market=primary, enabled_markets=list(enabled)),
    )
    monkeypatch.setattr(
        prefs, "is_market_enabled_now", lambda market: market is None or market.upper() in enabled
    )


def _prepare_refresh(monkeypatch, module, *, github_markets=None, github_statuses=None):
    _patch_serialized_lock(monkeypatch)
    monkeypatch.setattr(module, "SessionLocal", _universe_session_factory())
    monkeypatch.setattr(module, "get_ticker_validation_service", lambda: MagicMock())
    monkeypatch.setattr(
        module.calculate_eps_rating_percentiles, "delay", lambda: SimpleNamespace(id="eps")
    )
    for name in (
        "mark_market_activity_started",
        "mark_market_activity_completed",
        "mark_market_activity_failed",
        "mark_market_activity_progress",
    ):
        monkeypatch.setattr(module, name, lambda *args, **kwargs: None)
    synced = github_markets if github_markets is not None else []
    statuses = github_statuses or {}
    monkeypatch.setattr(
        module,
        "get_provider_snapshot_service",
        lambda: SimpleNamespace(
            sync_weekly_reference_from_github=(
                lambda db, market, **kwargs: synced.append(market)
                or {"status": statuses.get(market, "missing"), "market": market}
            )
        ),
    )


def _recording_cache(fetched):
    return SimpleNamespace(
        get_fundamentals=lambda symbol, **kwargs: fetched.append(symbol) or {"symbol": symbol}
    )


def test_unscoped_fundamentals_refresh_skips_disabled_markets(monkeypatch):
    import app.tasks.fundamentals_tasks as module

    _runtime_markets(monkeypatch, "US", "HK")
    _prepare_refresh(monkeypatch, module)
    fetched: list[str] = []
    monkeypatch.setattr(module, "get_fundamentals_cache", lambda: _recording_cache(fetched))

    result = module.refresh_all_fundamentals.run()

    assert sorted(fetched) == ["0700.HK", "AAPL"]
    assert result["total_stocks"] == 2


def test_scoped_refresh_fails_open_when_preferences_unreadable(monkeypatch):
    """An unreadable preference must not abort a scoped refresh (documented fail-open)."""
    import app.services.runtime_preferences_service as prefs
    import app.tasks.fundamentals_tasks as module

    def _unreadable(*args, **kwargs):
        raise ValueError("Unsupported market 'XX'")

    monkeypatch.setattr(prefs, "runtime_preferences_now", _unreadable)
    monkeypatch.setattr(prefs, "is_market_enabled_now", _unreadable)
    _prepare_refresh(monkeypatch, module)
    fetched: list[str] = []
    monkeypatch.setattr(module, "get_fundamentals_cache", lambda: _recording_cache(fetched))

    module.refresh_all_fundamentals.run(market="HK")

    assert fetched == ["0700.HK"]


@pytest.mark.parametrize(
    "statuses, expected_fetch",
    [
        ({"HK": "success", "US": "up_to_date"}, []),       # every enabled bundle synced: done
        ({"HK": "success", "US": "missing"}, ["AAPL"]),    # only the unsynced market is fetched
    ],
)
def test_unscoped_github_sync_covers_every_enabled_market(monkeypatch, statuses, expected_fetch):
    """The GitHub fast path must not complete an unscoped refresh after syncing only the primary market."""
    import app.tasks.fundamentals_tasks as module

    _runtime_markets(monkeypatch, "HK", "US", primary="HK")
    synced: list[str] = []
    _prepare_refresh(monkeypatch, module, github_markets=synced, github_statuses=statuses)
    fetched: list[str] = []
    monkeypatch.setattr(module, "get_fundamentals_cache", lambda: _recording_cache(fetched))

    module.refresh_all_fundamentals.run()

    assert sorted(synced) == ["HK", "US"]
    assert sorted(fetched) == expected_fetch


def test_unscoped_refresh_uses_primary_market_when_us_disabled(monkeypatch):
    """A non-US install must not sync US reference data."""
    import app.tasks.fundamentals_tasks as module

    _runtime_markets(monkeypatch, "HK", primary="HK")
    synced: list[str] = []
    _prepare_refresh(monkeypatch, module, github_markets=synced)
    fetched: list[str] = []
    monkeypatch.setattr(module, "get_fundamentals_cache", lambda: _recording_cache(fetched))

    module.refresh_all_fundamentals.run()

    assert synced == ["HK"]
    assert fetched == ["0700.HK"]
