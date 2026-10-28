"""Regression tests: the MCP candidate record is a projection of the schema.

The published feature run already carries the strategy ratings, the Setup
Engine facts and the VCP detail block. Instead of pinning those keys one by one
- which is a second, hand-maintained field list - the candidate record is
compared against the shared ``ScanResultItem`` projection. A fact that is in the
schema therefore reaches the MCP without a second edit, and a fact that is
missing from the schema is named as such here.

Nothing in this file recomputes a value: every expectation is either the seeded
row or the schema projection of it.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.infra.db.models.feature_store import FeatureRunPointer
from app.infra.serialization import vcp_num_bases_from_details
from app.interfaces.mcp.market_copilot import CANDIDATE_FIELDS, MarketCopilotService
from app.schemas.scanning import ScanResultItem
from tests.helpers.mcp_fixture import (
    create_mcp_test_session_factory,
    seed_market_copilot_data,
)


@pytest.fixture()
def session_factory():
    """In-memory database carrying the shared MCP seed data."""
    factory, _engine = create_mcp_test_session_factory()
    seed_market_copilot_data(factory)
    return factory


@pytest.fixture()
def service(session_factory):
    """Read-only copilot service over the seeded session factory."""
    return MarketCopilotService(
        session_factory,
        SimpleNamespace(
            mcp_watchlist_writes_enabled=False,
            mcp_server_name="stockscreen-market-copilot",
        ),
    )


def _payload(service, tool_name: str, arguments: dict) -> dict:
    """Call an MCP tool and return the envelope, asserting it did not error."""
    result = service.call_tool(tool_name, arguments)
    assert result.get("isError") is not True
    return result["structuredContent"]


def _explain_result(service, symbol: str, depth: str = "full") -> dict:
    """Return the ``result`` payload of an ``explain_symbol`` call."""
    return _payload(service, "explain_symbol", {"symbol": symbol, "depth": depth})["result"]


def _candidate_item(service, run_id: int, symbol: str):
    """Return one domain row as the repository maps it."""
    with service._uow_scope() as uow:
        item = uow.feature_store.get_by_symbol_for_run(
            run_id,
            symbol,
            include_sparklines=False,
            include_setup_payload=False,
        )
    assert item is not None
    return item


def _candidate_record(service, run_id: int, symbol: str) -> dict:
    """Return the projected record straight from the repository mapping."""
    return service._candidate_record(_candidate_item(service, run_id, symbol))


def test_candidate_record_is_the_schema_projection(service):
    """One row, one expectation: the record equals the shared projection."""
    item = _candidate_item(service, 2, "NVDA")

    record = service._candidate_record(item)

    projection = ScanResultItem.from_domain(item, include_setup_payload=False).model_dump(mode="json")
    expected = {key: projection[key] for key in CANDIDATE_FIELDS}
    expected["composite_score"] = round(expected["composite_score"], 2)

    assert set(record) == set(CANDIDATE_FIELDS)
    assert record == expected, "the candidate record drifted from the schema projection"
    # The facts this change was about are really inside the projection.
    assert record["minervini_rating"] == "Strong Buy"
    assert record["canslim_rating"] == "Buy"
    assert record["volume_breakthrough_rating"] == "Strong Buy"
    assert record["vcp_detected"] is True
    assert record["vcp_num_bases"] == 3
    assert record["volume_surge"] == 2.4
    assert record["action_state"] == "watch"


def test_candidate_record_rounds_the_composite_score_to_two_places(service):
    """Clients see the same 2-decimal score the MCP has always returned."""
    record = service._candidate_record(_candidate_item(service, 2, "NVDA"))

    assert record["composite_score"] == 92.0
    assert isinstance(record["composite_score"], float)


def test_a_malformed_stored_projection_skips_the_row_and_names_it(service):
    """A row whose stored opportunity state no longer validates is dropped, named.

    The call keeps working: one corrupt row must not make the overview unusable.
    But it must not disappear silently either - the symbol is reported so the
    operator can look at the data.
    """
    item = _candidate_item(service, 2, "NVDA")
    item.extended_fields["opportunity_state"] = {"policy_version": "correction-survivors-v1"}

    records, skipped = service._candidate_records([item])

    assert records == []
    assert skipped == ["NVDA"]


def test_a_row_without_a_projection_is_not_skipped(service):
    """Rows that predate the projection are valid: all-null stays all-null."""
    item = _candidate_item(service, 2, "MSFT")
    for key in ("correction_survivor", "resilience_score", "action_state", "opportunity_state"):
        item.extended_fields.pop(key, None)

    records, skipped = service._candidate_records([item])

    assert skipped == []
    assert records[0]["symbol"] == "MSFT"
    assert records[0]["action_state"] is None
    assert records[0]["opportunity_state"] is None


def test_volume_surge_is_the_stored_ratio_not_a_flag(service):
    """Kept from the fixture fix: the value is a ratio, and both rows differ."""
    assert _candidate_record(service, 2, "NVDA")["volume_surge"] == 2.4
    assert _candidate_record(service, 2, "SNOW")["volume_surge"] == 1.2


def test_explain_symbol_full_works_without_the_global_pointer(service, session_factory):
    """No regression of the market-pointer fix: the global pointer is not needed.

    ``_point`` mirrors what the publish path writes; the global pointer is
    removed here, and the per-market pointer keeps ``explain_symbol`` working.
    The facts ride along because the result is built from the candidate record.
    """
    with session_factory() as db:
        db.query(FeatureRunPointer).filter(FeatureRunPointer.key == "latest_published").delete()
        db.add(FeatureRunPointer(key="latest_published_market:US", run_id=2))
        db.commit()

    result = _explain_result(service, "NVDA")

    assert result["vcp_num_bases"] == 3
    assert result["minervini_rating"] == "Strong Buy"
    assert result["se_setup_score"] == 87.0


VCP_INCOMPLETE_DETAILS = [
    pytest.param({}, id="empty-details"),
    pytest.param({"details": None}, id="details-not-a-mapping"),
    pytest.param({"details": {"screeners": {}}}, id="no-minervini-screener"),
    pytest.param({"details": {"screeners": {"minervini": {"details": {}}}}}, id="no-full-analysis"),
    pytest.param(
        {"details": {"screeners": {"minervini": {"details": {"full_analysis": {"vcp": {}}}}}}},
        id="no-num-bases",
    ),
    pytest.param(
        {
            "details": {
                "screeners": {
                    "minervini": {
                        "details": {"full_analysis": {"vcp": {"num_bases": None}}}
                    }
                }
            }
        },
        id="num-bases-null",
    ),
    pytest.param(
        {
            "details": {
                "screeners": {
                    "minervini": {
                        "details": {"full_analysis": {"vcp": {"num_bases": "5"}}}
                    }
                }
            }
        },
        id="num-bases-string",
    ),
    pytest.param(
        {
            "details": {
                "screeners": {
                    "minervini": {
                        "details": {"full_analysis": {"vcp": {"num_bases": True}}}
                    }
                }
            }
        },
        id="num-bases-bool",
    ),
    pytest.param({"details": {"screeners": ["minervini"]}}, id="screeners-not-a-mapping"),
]


@pytest.mark.parametrize("details", VCP_INCOMPLETE_DETAILS)
def test_vcp_num_bases_is_none_when_the_stored_path_is_incomplete(details):
    """Missing nodes, non-mappings and non-integral values all yield ``None``.

    The boolean case matters most: ``bool`` is an ``int`` subclass, so a plain
    ``isinstance(value, int)`` check would report ``True`` as a base count.
    """
    assert vcp_num_bases_from_details(details) is None


def _vcp_details(num_bases):
    """Build the persisted details blob the VCP base count is read from."""
    return {
        "details": {
            "screeners": {
                "minervini": {"details": {"full_analysis": {"vcp": {"num_bases": num_bases}}}}
            }
        }
    }


@pytest.mark.parametrize(
    "value,expected",
    [
        pytest.param(3, 3, id="plain-int"),
        pytest.param(0, 0, id="zero-is-a-value"),
        pytest.param(5.0, 5, id="integral-float"),
    ],
)
def test_vcp_num_bases_reads_the_stored_integer(value, expected):
    """Stored counts pass through unchanged, including an integral float."""
    assert vcp_num_bases_from_details(_vcp_details(value)) == expected


def test_vcp_num_bases_rejects_a_fractional_count():
    """A fractional float is not a base count and must not be truncated."""
    assert vcp_num_bases_from_details(_vcp_details(2.5)) is None


@pytest.mark.parametrize("value", [pytest.param(-1, id="int"), pytest.param(-1.0, id="float")])
def test_vcp_num_bases_rejects_a_negative_count(value):
    """A count is never negative; both numeric branches must reject it.

    A negative stored value would otherwise reach ``extended_fields`` on both
    scan mappers and be handed to MCP clients as a base count.
    """
    assert vcp_num_bases_from_details(_vcp_details(value)) is None
