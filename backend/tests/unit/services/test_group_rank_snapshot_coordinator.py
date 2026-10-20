from datetime import date
from unittest.mock import Mock, call

import pytest

from app.domain.relative_strength import (
    BALANCED_RS_FORMULA_VERSION,
    BALANCED_RS_PRICE_BASIS,
    BALANCED_RS_SNAPSHOT_SCHEMA_VERSION,
    GroupSnapshotIdentity,
)
from app.infra.db.models.relative_strength import MarketRsRun
from app.infra.db.repositories.market_rs_repo import MarketRsRunRepository
from app.models.industry import IBDGroupRank, IBDIndustryGroup
from app.models.stock_universe import StockUniverse
from app.services.canonical_group_ranking_service import (
    CanonicalGroupRankingService,
    CanonicalGroupRankingUnavailable,
)
from app.services.group_rank_snapshot_coordinator import (
    GroupRankSnapshotCoordinator,
    GroupSnapshotStatus,
)
from app.services.group_rank_snapshot_reader import GroupRankSnapshotReader
from app.services.market_rs_inputs import MarketRsInputs
from app.services.market_rs_snapshot_service import MarketRsSnapshotService

AS_OF = date(2026, 4, 10)


class _CompleteMarketRsInputLoader:
    @staticmethod
    def load(_db, *, market, as_of_date):
        return MarketRsInputs(
            market=market,
            as_of_date=as_of_date,
            benchmark_symbol="SPY",
            benchmark_as_of_date=as_of_date,
            universe_hash="replacement-universe",
            expected_symbols=("AAA", "BBB", "CCC"),
            excess_returns_by_symbol={
                "AAA": {
                    "1d": 0.3,
                    "1w": 0.3,
                    "1m": 0.3,
                    "3m": 0.3,
                    "6m": 0.3,
                    "9m": 0.3,
                    "12m": 0.3,
                },
                "BBB": {
                    "1d": 0.2,
                    "1w": 0.2,
                    "1m": 0.2,
                    "3m": 0.2,
                    "6m": 0.2,
                    "9m": 0.2,
                    "12m": 0.2,
                },
                "CCC": {
                    "1d": 0.1,
                    "1w": 0.1,
                    "1m": 0.1,
                    "3m": 0.1,
                    "6m": 0.1,
                    "9m": 0.1,
                    "12m": 0.1,
                },
            },
            exclusions={},
            current_price_coverage=1.0,
        )


def _coordinator(reader, stock, canonical):
    return GroupRankSnapshotCoordinator(
        reader=reader,
        market_rs_snapshot_service=stock,
        canonical_group_service=canonical,
    )


def test_balanced_snapshot_publishes_stock_run_then_groups(db_session):
    reader = Mock()
    reader.load_exact.side_effect = [[], [{"market_rs_run_id": 44}]]
    stock = Mock()
    stock.calculate.return_value.id = 44
    canonical = Mock()
    identity = GroupSnapshotIdentity("US", AS_OF, BALANCED_RS_FORMULA_VERSION)

    result = _coordinator(reader, stock, canonical).ensure_snapshot(
        db_session, identity=identity
    )

    assert result.status is GroupSnapshotStatus.PROCESSED
    stock.calculate.assert_called_once_with(
        db_session,
        market="US",
        as_of_date=AS_OF,
        formula_version=BALANCED_RS_FORMULA_VERSION,
        rebuild_incompatible=True,
    )
    canonical.calculate_and_store.assert_called_once()


def test_balanced_snapshot_rebuilds_incompatible_completed_stock_run(db_session):
    old_run = MarketRsRun(
        market="US",
        as_of_date=AS_OF,
        formula_version=BALANCED_RS_FORMULA_VERSION,
        status="completed",
        benchmark_symbol="SPY",
        benchmark_as_of_date=AS_OF,
        universe_hash="legacy-upgrade-run",
        expected_symbol_count=3,
        eligible_symbol_count=3,
        excluded_symbol_count=0,
        diagnostics_json={"price_basis": BALANCED_RS_PRICE_BASIS},
    )
    db_session.add(old_run)
    db_session.flush()
    old_run_id = old_run.id
    for symbol in ("AAA", "BBB", "CCC"):
        db_session.add(
            StockUniverse(
                symbol=symbol,
                name=f"{symbol} Inc.",
                market="US",
                exchange="NASDAQ",
                market_cap=100.0,
                is_active=True,
                status="active",
            )
        )
        db_session.add(
            IBDIndustryGroup(
                symbol=symbol,
                industry_group="Software",
                market="US",
                source="manual",
            )
        )
    db_session.commit()

    repository = MarketRsRunRepository()
    coordinator = GroupRankSnapshotCoordinator(
        reader=GroupRankSnapshotReader(),
        market_rs_snapshot_service=MarketRsSnapshotService(
            input_loader=_CompleteMarketRsInputLoader(),
            repository=repository,
        ),
        canonical_group_service=CanonicalGroupRankingService(repository=repository),
    )
    identity = GroupSnapshotIdentity("US", AS_OF, BALANCED_RS_FORMULA_VERSION)

    result = coordinator.ensure_snapshot(db_session, identity=identity)

    assert result.status is GroupSnapshotStatus.PROCESSED
    assert result.row_count == 1
    assert result.market_rs_run_id != old_run_id
    assert result.market_rs_run_id is not None
    rebuilt = repository.get_completed_exact(
        db_session,
        market="US",
        as_of_date=AS_OF,
        formula_version=BALANCED_RS_FORMULA_VERSION,
    )
    assert rebuilt is not None
    assert rebuilt.id == result.market_rs_run_id
    assert (
        rebuilt.diagnostics_json["rs_snapshot_schema_version"]
        == BALANCED_RS_SNAPSHOT_SCHEMA_VERSION
    )


def test_backfill_rolls_back_failed_date_before_processing_next(db_session):
    coordinator = _coordinator(Mock(), Mock(), Mock())
    first = GroupSnapshotIdentity("US", date(2026, 4, 9), BALANCED_RS_FORMULA_VERSION)
    second = GroupSnapshotIdentity("US", AS_OF, BALANCED_RS_FORMULA_VERSION)
    coordinator.ensure_snapshot = Mock(
        side_effect=[
            RuntimeError("database aborted"),
            Mock(
                status=GroupSnapshotStatus.PROCESSED,
                row_count=3,
                market_rs_run_id=8,
            ),
        ]
    )
    db_session.rollback = Mock(wraps=db_session.rollback)

    report = coordinator.backfill(
        db_session,
        identities=(first, second),
        continue_on_error=True,
    )

    assert db_session.rollback.call_count == 1
    assert [item.status for item in report.results] == [
        GroupSnapshotStatus.ERRORED,
        GroupSnapshotStatus.PROCESSED,
    ]
    assert coordinator.ensure_snapshot.call_args_list == [
        call(db_session, identity=first),
        call(db_session, identity=second),
    ]


def test_repair_snapshot_rebuilds_exact_balanced_identity(db_session):
    identity = GroupSnapshotIdentity("US", AS_OF, BALANCED_RS_FORMULA_VERSION)
    reader = Mock()
    reader.load_exact.return_value = [
        {"market_rs_run_id": 42, "industry_group": "Software"}
    ]
    stock = Mock()
    stock.rebuild_incompatible_staged.return_value.id = 42
    canonical = Mock()

    result = _coordinator(reader, stock, canonical).repair_snapshot(
        db_session,
        identity=identity,
    )

    assert result.status is GroupSnapshotStatus.PROCESSED
    stock.rebuild_incompatible_staged.assert_called_once_with(
        db_session,
        market="US",
        as_of_date=AS_OF,
        formula_version=BALANCED_RS_FORMULA_VERSION,
    )
    canonical.calculate_and_stage.assert_called_once_with(
        db_session,
        market="US",
        as_of_date=AS_OF,
        formula_version=BALANCED_RS_FORMULA_VERSION,
    )


def test_repair_snapshot_rejects_empty_balanced_replacement(db_session):
    identity = GroupSnapshotIdentity("US", AS_OF, BALANCED_RS_FORMULA_VERSION)
    reader = Mock()
    stock = Mock()
    stock.rebuild_incompatible_staged.return_value.id = 42
    canonical = Mock()
    canonical.calculate_and_stage.return_value = []

    with pytest.raises(RuntimeError, match="produced no rankings"):
        _coordinator(reader, stock, canonical).repair_snapshot(
            db_session,
            identity=identity,
        )

    reader.load_exact.assert_not_called()


def test_failed_balanced_repair_preserves_previous_run_and_group_rows(db_session):
    old_run = MarketRsRun(
        market="US",
        as_of_date=AS_OF,
        formula_version=BALANCED_RS_FORMULA_VERSION,
        status="completed",
        benchmark_symbol="SPY",
        benchmark_as_of_date=AS_OF,
        universe_hash="incompatible-universe",
        expected_symbol_count=3,
        eligible_symbol_count=3,
        excluded_symbol_count=0,
        diagnostics_json={},
    )
    db_session.add(old_run)
    db_session.flush()
    old_run_id = old_run.id
    db_session.add(
        IBDGroupRank(
            market="US",
            industry_group="Preserved Group",
            date=AS_OF,
            rank=1,
            avg_rs_rating=75.0,
            num_stocks=3,
            rs_formula_version=BALANCED_RS_FORMULA_VERSION,
            market_rs_run_id=old_run_id,
        )
    )
    db_session.commit()

    repository = MarketRsRunRepository()
    coordinator = GroupRankSnapshotCoordinator(
        reader=GroupRankSnapshotReader(),
        market_rs_snapshot_service=MarketRsSnapshotService(
            input_loader=_CompleteMarketRsInputLoader(),
            repository=repository,
        ),
        canonical_group_service=CanonicalGroupRankingService(repository=repository),
    )
    identity = GroupSnapshotIdentity(
        "US",
        AS_OF,
        BALANCED_RS_FORMULA_VERSION,
    )

    with pytest.raises(CanonicalGroupRankingUnavailable):
        coordinator.repair_snapshot(db_session, identity=identity)
    db_session.rollback()

    preserved_run = repository.get_completed_exact(
        db_session,
        market="US",
        as_of_date=AS_OF,
        formula_version=BALANCED_RS_FORMULA_VERSION,
    )
    preserved_group = db_session.query(IBDGroupRank).one()
    assert preserved_run is not None
    assert preserved_run.id == old_run_id
    assert preserved_group.industry_group == "Preserved Group"
    assert preserved_group.market_rs_run_id == old_run_id
