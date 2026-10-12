"""Tools exercised end to end through the SDK's in-memory client."""

import pytest
from mcp import Client

from stockscreen_mcp import server

pytestmark = [pytest.mark.anyio, pytest.mark.usefixtures("use_fake_data")]


async def call(tool, **arguments):
    async with Client(server.mcp) as client:
        result = await client.call_tool(tool, arguments)
    assert not result.is_error, result.content
    return result.structured_content


async def call_error(tool, **arguments):
    async with Client(server.mcp) as client:
        result = await client.call_tool(tool, arguments)
    assert result.is_error
    return result.content[0].text


async def test_every_tool_is_listed_as_read_only():
    async with Client(server.mcp) as client:
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}

    assert set(tools) == {
        "list_markets", "list_screens", "run_screen", "get_stock", "market_overview", "group_rankings",
    }
    for tool in tools.values():
        assert tool.annotations.read_only_hint is True
        assert tool.description


async def test_list_markets_reports_dates_status_and_features():
    result = await call("list_markets")

    us = next(m for m in result["markets"] if m["code"] == "US")
    assert us == {
        "code": "US", "name": "United States", "as_of_date": "2026-10-09", "status": "current",
        "features": ["breadth", "groups", "scan"],
    }
    assert result["unavailable_markets"] == ["IN"]


async def test_list_screens_hides_opportunity_presets_where_the_site_does():
    us = {s["id"] for s in (await call("list_screens", market="US"))["screens"]}
    au = {s["id"] for s in (await call("list_screens", market="AU"))["screens"]}

    assert us == {"minervini", "correction_survivors", "se_cup_handle"}
    assert au == {"minervini", "se_cup_handle"}


async def test_preset_screen_matches_and_sorts_like_the_site():
    result = await call("run_screen", market="US", screen="minervini")

    assert result["screen"] == "Minervini Trend Template"
    assert result["total_matches"] == 2
    assert [row["symbol"] for row in result["rows"]] == ["AAA", "BBB"]
    assert result["sorted_by"] == "minervini_score desc"
    first = result["rows"][0]
    assert first["rs_rating"] == 95 and first["minervini_score"] == 90
    assert "price_sparkline_data" not in first


async def test_custom_filters_combine_with_a_preset_and_show_their_fields():
    result = await call(
        "run_screen", market="US", screen="minervini",
        filters=[{"field": "ibd_group_rank", "max": 10}],
    )

    assert [row["symbol"] for row in result["rows"]] == ["AAA"]
    assert result["rows"][0]["ibd_group_rank"] == 3


async def test_filters_alone_use_the_market_default_sort_and_respect_limit():
    result = await call("run_screen", market="US", filters=[{"field": "stage", "equals": 2}], limit=2)

    assert result["total_matches"] == 4
    assert [row["symbol"] for row in result["rows"]] == ["AAA", "DDD"]
    assert result["sorted_by"] == "composite_score desc"


async def test_unknown_screen_and_unknown_field_are_explained():
    assert "minervini" in await call_error("run_screen", market="US", screen="minerva")
    message = await call_error("run_screen", market="US", filters=[{"field": "rs_ratin", "min": 90}])
    assert "rs_ratin" in message and "rs_rating" in message
    assert "Pass a screen" in await call_error("run_screen", market="US")


async def test_get_stock_returns_the_row_and_the_screens_it_passes():
    result = await call("get_stock", symbol="aaa", market="US")

    assert result["symbol"] == "AAA"
    assert result["rs_rating"] == 95
    assert result["passes_screens"] == ["minervini", "correction_survivors", "se_cup_handle"]
    assert "field_availability" not in result
    assert "not found" in await call_error("get_stock", symbol="ZZZ", market="US")


async def test_market_overview_summarises_health_breadth_and_groups():
    result = await call("market_overview", market="US")

    assert result["exposure"]["exposure_score"] == 45.0
    assert result["exposure"]["stance"] == "Downtrend/Caution"
    assert "history" not in result["exposure"]
    assert result["breadth"]["latest"]["stocks_up_4pct"] == 29
    assert "eligibility_signature" not in result["breadth"]["latest"]
    assert [day["date"] for day in result["breadth"]["recent"]] == [
        "2026-10-09", "2026-10-08", "2026-10-07", "2026-10-06", "2026-10-05",
    ]
    assert result["key_markets"][0] == {
        "symbol": "SPY", "name": "S&P 500 ETF", "close": 778.57, "change_1d_pct": 0.6, "date": "2026-10-09",
    }
    assert result["top_groups"][0]["group"] == "Semiconductors"


async def test_market_overview_says_when_breadth_is_not_published():
    result = await call("market_overview", market="AU")

    assert result["breadth"] == {"available": False, "reason": "Breadth is not published for AU."}


async def test_group_rankings_list_and_one_group_in_detail():
    ranked = await call("group_rankings", market="US")
    assert [g["group"] for g in ranked["groups"]] == ["Semiconductors", "Software"]
    assert ranked["movers_1w"]["losers"][0] == {"group": "Software", "rank": 2, "rank_change_1w": -30}

    detail = await call("group_rankings", market="US", group="semiconductors", limit=1)
    assert detail["group"]["group"] == "Semiconductors"
    assert [s["symbol"] for s in detail["group"]["stocks"]] == ["CCC"]  # highest RS first
    assert "price_sparkline_data" not in detail["group"]["stocks"][0]
    assert detail["group"]["rank_history"][0] == {"date": "2026-10-09", "rank": 9, "avg_rs_rating": 89.0}
    assert len(detail["group"]["rank_history"]) == 8
    assert "Semiconductors" in await call_error("group_rankings", market="US", group="Semis Typo")


async def test_group_lookup_accepts_a_unique_partial_name():
    # IBD names are abbreviated ("Elec-Semiconductor Equip"); a unique substring should do.
    detail = await call("group_rankings", market="US", group="semiconduct")
    assert detail["group"]["group"] == "Semiconductors"
