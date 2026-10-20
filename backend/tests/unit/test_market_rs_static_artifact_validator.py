"""Strict validation coverage for staged canonical Market RS artifacts."""

from __future__ import annotations

import json
from datetime import date
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.domain.relative_strength import BALANCED_RS_FORMULA_VERSION, StockRsPlacement
from app.services.market_rs_static_artifact_validator import (
    MarketRsStaticArtifactValidator,
    StaticRsArtifactDocuments,
)
from app.services.static_groups_rrg_export import (
    StaticGroupsRRGUnavailableError,
    StaticGroupsRRGUnavailableReason,
)
from app.services.static_site_export_service import (
    SCAN_BUNDLE_SCHEMA_VERSION,
    STATIC_SITE_SCHEMA_VERSION,
)


def _write_json(path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _staged_bundle(
    tmp_path,
    *,
    market: str = "US",
    groups_available: bool = True,
    second_row_metadata: dict | None = None,
):
    market_lower = market.lower()
    market_dir = tmp_path / "markets" / market_lower
    identity = {
        "rs_formula_version": BALANCED_RS_FORMULA_VERSION,
        "market_rs_run_id": 42,
        "rs_as_of_date": "2026-04-10",
        "rs_universe_size": 2,
    }
    _write_json(
        tmp_path / "manifest.json",
        {
            "schema_version": STATIC_SITE_SCHEMA_VERSION,
            "markets": {market: dict(identity)},
        },
    )
    groups_payload = (
        {
            "schema_version": STATIC_SITE_SCHEMA_VERSION,
            **identity,
            "payload": {"rankings": {"rankings": []}},
        }
        if groups_available
        else {
            "schema_version": STATIC_SITE_SCHEMA_VERSION,
            "available": False,
            "message": f"No group rankings are available for {market}.",
            "payload": {},
        }
    )
    _write_json(
        market_dir / "groups.json",
        groups_payload,
    )
    rows = [
        {
            "symbol": "AAA",
            "rs_rating": 90,
            "rs_rating_1m": 88,
            "rs_rating_3m": 89,
            "rs_rating_12m": 91,
            **identity,
        },
        {
            "symbol": "BBB",
            "rs_rating": 80,
            "rs_rating_1m": 78,
            "rs_rating_3m": 79,
            "rs_rating_12m": 81,
            **identity,
            **(second_row_metadata or {}),
        },
    ]
    chunks = []
    for index, row in enumerate(rows, start=1):
        relative = f"markets/{market_lower}/scan/chunks/chunk-{index:04d}.json"
        chunks.append({"path": relative, "count": 1})
        _write_json(
            tmp_path / relative,
            {
                "schema_version": SCAN_BUNDLE_SCHEMA_VERSION,
                "run_id": 99,
                "chunk_index": index,
                **identity,
                "rows": [row],
            },
        )
    _write_json(
        market_dir / "scan" / "manifest.json",
        {
            "schema_version": SCAN_BUNDLE_SCHEMA_VERSION,
            "run_id": 99,
            "rows_total": 2,
            "chunks": chunks,
            **identity,
        },
    )
    return market_dir


def _latest_run():
    return SimpleNamespace(
        id=42,
        eligible_symbol_count=2,
        rows=[
            SimpleNamespace(
                symbol="AAA", overall_rs=90, rs_1m=88, rs_3m=89, rs_12m=91
            ),
            SimpleNamespace(
                symbol="BBB", overall_rs=80, rs_1m=78, rs_3m=79, rs_12m=81
            ),
        ],
        placements=[],
    )


def test_bundle_fingerprint_covers_market_files_not_only_root_manifest(tmp_path):
    market_dir = _staged_bundle(tmp_path)

    before = MarketRsStaticArtifactValidator.bundle_fingerprint(
        tmp_path,
        market="US",
    )
    groups_path = market_dir / "groups.json"
    groups_path.write_text(groups_path.read_text() + "\n", encoding="utf-8")
    after = MarketRsStaticArtifactValidator.bundle_fingerprint(
        tmp_path,
        market="US",
    )

    assert before.sha256 != after.sha256
    assert "markets/us/groups.json" in before.files


def test_validate_checks_every_scan_shard_and_requires_exact_row_identity(
    tmp_path,
    monkeypatch,
):
    _staged_bundle(
        tmp_path,
        second_row_metadata={"market_rs_run_id": None},
    )
    validator = MarketRsStaticArtifactValidator(stream_stock_rows=False)
    monkeypatch.setattr(validator, "_validate_group_parity", lambda *a, **k: None)
    monkeypatch.setattr(validator, "_validate_rrg", lambda *a, **k: "available")

    result = validator.validate(
        MagicMock(),
        market="US",
        through_date=date(2026, 4, 10),
        latest_run=_latest_run(),
        feature_run_id=99,
        static_staging_dir=tmp_path,
    )

    assert any(
        "BBB.market_rs_run_id" in error and "expected 42" in error
        for error in result.errors
    )
    assert result.bundle_fingerprint is not None


def test_load_scan_rows_returns_stream_source_instead_of_materialized_tuple(
    tmp_path,
):
    market_dir = _staged_bundle(tmp_path)
    scan_payload = json.loads(
        (market_dir / "scan" / "manifest.json").read_text(encoding="utf-8")
    )
    identity = {
        "rs_formula_version": BALANCED_RS_FORMULA_VERSION,
        "market_rs_run_id": 42,
        "rs_as_of_date": "2026-04-10",
        "rs_universe_size": 2,
    }
    errors: list[str] = []

    scan_rows = MarketRsStaticArtifactValidator()._load_scan_rows(
        tmp_path,
        market_dir=market_dir,
        scan_payload=scan_payload,
        feature_run_id=99,
        expected_identity=identity,
        errors=errors,
    )

    assert errors == []
    assert not isinstance(scan_rows, tuple)
    assert scan_rows.row_count == 2
    assert [row["symbol"] for row in scan_rows.iter_rows()] == ["AAA", "BBB"]
    assert [row["symbol"] for row in scan_rows.iter_rows()] == ["AAA", "BBB"]


def test_stock_parity_allows_audited_rows_excluded_from_canonical_ratings():
    latest_run = SimpleNamespace(
        id=42,
        eligible_symbol_count=1,
        rows=[
            SimpleNamespace(
                symbol="AAA",
                overall_rs=90,
                rs_1m=88,
                rs_3m=89,
                rs_12m=91,
            )
        ],
        placements=[],
    )
    identity = {
        "rs_formula_version": BALANCED_RS_FORMULA_VERSION,
        "market_rs_run_id": 42,
        "rs_as_of_date": "2026-04-10",
        "rs_universe_size": 1,
    }
    documents = StaticRsArtifactDocuments(
        manifest={},
        groups={},
        scan_rows=(
            {
                "symbol": "AAA",
                "rs_rating": 90,
                "rs_rating_1m": 88,
                "rs_rating_3m": 89,
                "rs_rating_12m": 91,
                **identity,
            },
            {
                "symbol": "YOUNG",
                "rs_rating": None,
                "rs_rating_1m": None,
                "rs_rating_3m": None,
                "rs_rating_12m": None,
                **identity,
            },
        ),
    )
    errors: list[str] = []

    MarketRsStaticArtifactValidator(stream_stock_rows=False)._validate_stock_parity(
        latest_run=latest_run,
        through_date=date(2026, 4, 10),
        documents=documents,
        errors=errors,
    )

    assert errors == []


def test_stock_parity_streams_rows_when_enabled_for_session_like_db():
    class _LatestRun:
        id = 42
        eligible_symbol_count = 1

        @property
        def rows(self):
            raise AssertionError("streamed validation must not hydrate run rows")

    latest_run = _LatestRun()
    identity = {
        "rs_formula_version": BALANCED_RS_FORMULA_VERSION,
        "market_rs_run_id": 42,
        "rs_as_of_date": "2026-04-10",
        "rs_universe_size": 1,
    }
    documents = StaticRsArtifactDocuments(
        manifest={},
        groups={},
        scan_rows=(
            {
                "symbol": "AAA",
                "rs_rating": 90,
                "rs_rating_1m": 88,
                "rs_rating_3m": 89,
                "rs_rating_12m": 91,
                **identity,
            },
        ),
    )
    repository = MagicMock()
    repository.iter_stock_rows_for_run.return_value = (
        SimpleNamespace(
            symbol="AAA",
            overall_rs=90,
            rs_1m=88,
            rs_3m=89,
            rs_12m=91,
        )
        for _ in range(1)
    )
    validator = MarketRsStaticArtifactValidator(
        market_rs_repository=repository,
        stream_stock_rows=True,
    )
    errors: list[str] = []
    db = object()

    validator._validate_stock_parity(
        db=db,
        latest_run=latest_run,
        through_date=date(2026, 4, 10),
        documents=documents,
        errors=errors,
    )

    assert errors == []
    repository.iter_stock_rows_for_run.assert_called_once_with(db, run_id=42)


def test_stock_parity_uses_latest_run_rows_when_streaming_disabled():
    latest_run = SimpleNamespace(
        id=42,
        eligible_symbol_count=1,
        rows=[
            SimpleNamespace(
                symbol="AAA",
                overall_rs=90,
                rs_1m=88,
                rs_3m=89,
                rs_12m=91,
            )
        ],
        placements=[],
    )
    identity = {
        "rs_formula_version": BALANCED_RS_FORMULA_VERSION,
        "market_rs_run_id": 42,
        "rs_as_of_date": "2026-04-10",
        "rs_universe_size": 1,
    }
    documents = StaticRsArtifactDocuments(
        manifest={},
        groups={},
        scan_rows=(
            {
                "symbol": "AAA",
                "rs_rating": 90,
                "rs_rating_1m": 88,
                "rs_rating_3m": 89,
                "rs_rating_12m": 91,
                **identity,
            },
        ),
    )
    repository = MagicMock()
    repository.iter_stock_rows_for_run.side_effect = AssertionError(
        "streaming is disabled for this validation"
    )
    validator = MarketRsStaticArtifactValidator(
        market_rs_repository=repository,
        stream_stock_rows=False,
    )
    errors: list[str] = []

    validator._validate_stock_parity(
        db=MagicMock(),
        latest_run=latest_run,
        through_date=date(2026, 4, 10),
        documents=documents,
        errors=errors,
    )

    assert errors == []
    repository.iter_stock_rows_for_run.assert_not_called()


def test_rrg_validation_accepts_market_where_rrg_is_not_enabled(
    tmp_path,
    monkeypatch,
):
    class _UnavailableSource:
        def __init__(self, **_kwargs):
            pass

        def build(self, **_kwargs):
            raise StaticGroupsRRGUnavailableError(
                section="CA rrg",
                reason_code=StaticGroupsRRGUnavailableReason.NOT_ENABLED,
                reason="RRG is not enabled for market CA.",
            )

    monkeypatch.setattr(
        "app.services.market_rs_static_artifact_validator."
        "StaticGroupsRRGDatabasePayloadSource",
        _UnavailableSource,
    )
    errors: list[str] = []

    status = MarketRsStaticArtifactValidator()._validate_rrg(
        MagicMock(),
        market="CA",
        through_date=date(2026, 4, 10),
        market_dir=tmp_path,
        errors=errors,
    )

    assert status == "not_enabled"
    assert errors == []


def test_rrg_validation_rejects_insufficient_history_for_enabled_market(
    tmp_path,
    monkeypatch,
):
    class _UnavailableSource:
        def __init__(self, **_kwargs):
            pass

        def build(self, **_kwargs):
            raise StaticGroupsRRGUnavailableError(
                section="US rrg",
                reason_code=StaticGroupsRRGUnavailableReason.INSUFFICIENT_HISTORY,
                reason="Only 11 usable weeks are available.",
            )

    monkeypatch.setattr(
        "app.services.market_rs_static_artifact_validator."
        "StaticGroupsRRGDatabasePayloadSource",
        _UnavailableSource,
    )
    errors: list[str] = []

    status = MarketRsStaticArtifactValidator()._validate_rrg(
        MagicMock(),
        market="US",
        through_date=date(2026, 4, 10),
        market_dir=tmp_path,
        errors=errors,
    )

    assert status == "insufficient_balanced_history"
    assert errors == [
        "Balanced RRG history is insufficient for guarded activation: "
        "Only 11 usable weeks are available."
    ]


def test_validate_accepts_unavailable_groups_for_group_less_market(
    tmp_path,
    monkeypatch,
):
    _staged_bundle(tmp_path, market="DE", groups_available=False)
    validator = MarketRsStaticArtifactValidator(stream_stock_rows=False)
    validate_group_parity = MagicMock()
    monkeypatch.setattr(validator, "_validate_group_parity", validate_group_parity)
    monkeypatch.setattr(validator, "_validate_rrg", lambda *a, **k: "not_enabled")

    result = validator.validate(
        MagicMock(),
        market="DE",
        through_date=date(2026, 4, 10),
        latest_run=_latest_run(),
        feature_run_id=99,
        static_staging_dir=tmp_path,
    )

    assert result.errors == ()
    assert result.rrg_status == "not_enabled"
    validate_group_parity.assert_not_called()


# --- placed (young) stocks ---------------------------------------------------

_IDENTITY = {
    "rs_formula_version": BALANCED_RS_FORMULA_VERSION,
    "market_rs_run_id": 42,
    "rs_as_of_date": "2026-04-10",
    "rs_universe_size": 1,
}
_AAA_ROW = {
    "symbol": "AAA",
    "rs_rating": 90,
    "rs_rating_1m": 88,
    "rs_rating_3m": 89,
    "rs_rating_12m": 91,
    **_IDENTITY,
}
_YNG_FIELDS = {"rs_rating": None, "rs_rating_1m": 66, "rs_rating_3m": 1, "rs_rating_12m": None}


def _placement_row(symbol="YNG", **ratings):
    row = SimpleNamespace(symbol=symbol)
    for horizon in ("1d", "1w", "1m", "3m", "6m", "9m", "12m"):
        setattr(row, f"rs_{horizon}", ratings.get(horizon))
        setattr(row, f"excess_return_{horizon}", 0.05 if horizon in ratings else None)
    return row


def _run_with_placement(*, placements):
    return SimpleNamespace(
        id=42,
        eligible_symbol_count=1,
        rows=[SimpleNamespace(symbol="AAA", overall_rs=90, rs_1m=88, rs_3m=89, rs_12m=91)],
        placements=placements,
    )


def _placed_errors(latest_run, *rows):
    errors: list[str] = []
    MarketRsStaticArtifactValidator(stream_stock_rows=False)._validate_stock_parity(
        latest_run=latest_run,
        through_date=date(2026, 4, 10),
        documents=StaticRsArtifactDocuments(manifest={}, groups={}, scan_rows=tuple(rows)),
        errors=errors,
    )
    return errors


def _young_run():
    return _run_with_placement(
        placements=[_placement_row("YNG", **{"1d": 99, "1w": 99, "1m": 66, "3m": 1})]
    )


def test_stock_parity_accepts_a_placed_row_that_matches_its_placement():
    errors = _placed_errors(_young_run(), _AAA_ROW, {"symbol": "YNG", **_YNG_FIELDS, **_IDENTITY})

    assert errors == []


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"rs_rating_1m": 65}, ["Live/static placed stock mismatch for YNG.rs_rating_1m."]),
        ({"rs_rating": 50}, ["Live/static placed stock mismatch for YNG.rs_rating."]),
        (
            {"rs_rating_1m": None, "rs_rating_3m": None},
            [
                "Live/static placed stock mismatch for YNG.rs_rating_1m.",
                "Live/static placed stock mismatch for YNG.rs_rating_3m.",
            ],
        ),
        ({"rs_universe_size": 2}, ["Static stock YNG.rs_universe_size=2; expected 1."]),
    ],
)
def test_stock_parity_rejects_a_placed_row_that_differs_from_its_placement(overrides, expected):
    errors = _placed_errors(
        _young_run(), _AAA_ROW, {"symbol": "YNG", **_YNG_FIELDS, **_IDENTITY, **overrides}
    )

    assert errors == expected


def test_stock_parity_still_rejects_rs_on_a_symbol_the_run_never_rated():
    errors = _placed_errors(
        _young_run(),
        _AAA_ROW,
        {"symbol": "GAP", "rs_rating": None, "rs_rating_1m": 40, "rs_rating_3m": None,
         "rs_rating_12m": None, **_IDENTITY},
    )

    assert errors == [
        "Static stock GAP carries RS ratings but is absent from the canonical Market RS run."
    ]


def test_stock_parity_counts_placed_rows_as_overlap():
    errors = _placed_errors(_young_run(), {"symbol": "YNG", **_YNG_FIELDS, **_IDENTITY})

    assert errors == []


def test_stock_parity_treats_a_run_without_placements_as_today():
    old_run = _run_with_placement(placements=[])
    empty = {"rs_rating": None, "rs_rating_1m": None, "rs_rating_3m": None, "rs_rating_12m": None}

    assert _placed_errors(old_run, _AAA_ROW, {"symbol": "YNG", **empty, **_IDENTITY}) == []


def test_stock_parity_streams_placements_with_the_stock_rows():
    repository = MagicMock()
    repository.iter_stock_rows_for_run.return_value = iter(
        [SimpleNamespace(symbol="AAA", overall_rs=90, rs_1m=88, rs_3m=89, rs_12m=91)]
    )
    repository.iter_placements_for_run.return_value = iter(
        [StockRsPlacement("YNG", {"1d": 99, "1m": 66, "1w": 99, "3m": 1}, {h: 0.05 for h in ("1d", "1w", "1m", "3m")})]
    )
    validator = MarketRsStaticArtifactValidator(market_rs_repository=repository, stream_stock_rows=True)
    errors: list[str] = []
    db = object()

    validator._validate_stock_parity(
        db=db,
        latest_run=SimpleNamespace(id=42, eligible_symbol_count=1),
        through_date=date(2026, 4, 10),
        documents=StaticRsArtifactDocuments(
            manifest={},
            groups={},
            scan_rows=(_AAA_ROW, {"symbol": "YNG", **_YNG_FIELDS, "rs_rating_3m": 2, **_IDENTITY}),
        ),
        errors=errors,
    )

    assert errors == ["Live/static placed stock mismatch for YNG.rs_rating_3m."]
    repository.iter_placements_for_run.assert_called_once_with(db, run_id=42)
