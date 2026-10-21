import json

import pytest

from app.models.industry import IBDIndustryGroup
from app.services.static_group_matrix import (
    export_group_matrix,
    validate_group_matrix_asset,
)
from tests.unit.test_group_matrix_service import (
    add_run,
    matrix_db,  # noqa: F401
)


def test_export_freezes_selected_run_and_advertises_compact_asset(db, tmp_path):
    add_run(db, 1, "US", ["A"])
    add_run(db, 2, "US", ["B"])
    db.add(IBDIndustryGroup(symbol="A", market="US", industry_group="Software"))
    db.commit()
    asset = export_group_matrix(
        db, output_dir=tmp_path, market="US", feature_run_id=1, generated_at="now"
    )
    assert asset == {"path": "markets/us/groups_matrix.json"}
    payload = json.loads((tmp_path / asset["path"]).read_text())
    assert payload["feature_run_id"] == 1
    assert [s["symbol"] for s in payload["stocks"]] == ["A"]
    assert payload["stocks"][0]["price_change_1w"] == -1.25
    assert payload["stocks"][0]["price_change_1m"] == 9.5
    assert "price_sparkline_data" not in payload["stocks"][0]
    entry = {
        key: payload[key]
        for key in (
            "market",
            "feature_run_id",
            "as_of_date",
            "rs_formula_version",
            "market_rs_run_id",
            "rs_universe_size",
        )
    }
    entry["assets"] = {"groups_matrix": asset}
    validate_group_matrix_asset(
        market="US", market_dir=tmp_path / "markets/us", entry=entry
    )
    entry["feature_run_id"] = 2
    with pytest.raises(ValueError, match="identity"):
        validate_group_matrix_asset(
            market="US", market_dir=tmp_path / "markets/us", entry=entry
        )


def test_no_ibd_coverage_does_not_advertise_matrix(db, tmp_path):
    add_run(db, 1, "US", ["A"])
    assert (
        export_group_matrix(
            db, output_dir=tmp_path, market="US", feature_run_id=1, generated_at="now"
        )
        is None
    )
    assert list(tmp_path.rglob("*.json")) == []


@pytest.mark.parametrize(
    "path",
    ["../groups_matrix.json", "markets/hk/groups_matrix.json", "https://bad.test/map"],
)
def test_asset_path_cannot_escape_market(tmp_path, path):
    with pytest.raises(ValueError):
        validate_group_matrix_asset(
            market="US",
            market_dir=tmp_path,
            entry={"assets": {"groups_matrix": {"path": path}}},
        )


def test_old_manifest_without_matrix_is_supported(tmp_path):
    validate_group_matrix_asset(market="US", market_dir=tmp_path, entry={"assets": {}})


@pytest.mark.parametrize(
    "change",
    [
        {"schema_version": "unsupported"},
        {"market": "HK"},
        {"as_of_date": "2020-01-01"},
        {"stocks": []},
        {"available": False},
    ],
)
def test_combiner_rejects_inconsistent_advertised_matrix(db, tmp_path, change):
    from app.services.static_artifact_combiner import (
        StaticArtifactCombiner,
        StaticArtifactDamagedError,
    )

    add_run(db, 1, "US", ["A"])
    db.add(IBDIndustryGroup(symbol="A", market="US", industry_group="Software"))
    db.commit()
    asset = export_group_matrix(
        db, output_dir=tmp_path, market="US", feature_run_id=1, generated_at="now"
    )
    path = tmp_path / asset["path"]
    payload = json.loads(path.read_text())
    entry = {
        key: payload[key]
        for key in (
            "market",
            "feature_run_id",
            "as_of_date",
            "rs_formula_version",
            "market_rs_run_id",
            "rs_universe_size",
        )
    }
    entry["assets"] = {"groups_matrix": asset}
    path.write_text(json.dumps({**payload, **change}))
    with pytest.raises(StaticArtifactDamagedError):
        StaticArtifactCombiner._validate_advertised_assets(
            market="US", source_label="test", market_dir=path.parent, entry=entry
        )


@pytest.mark.parametrize("assets", ["invalid", ["groups_matrix"], [], False, 0, ""])
def test_combiner_reports_non_mapping_assets_as_invalid_artifacts(tmp_path, assets):
    from app.services.static_artifact_combiner import (
        StaticArtifactCombiner,
        StaticArtifactDamagedError,
    )

    with pytest.raises(StaticArtifactDamagedError, match="assets"):
        StaticArtifactCombiner._validate_advertised_assets(
            market="US", source_label="test", market_dir=tmp_path,
            entry={"assets": assets},
        )
