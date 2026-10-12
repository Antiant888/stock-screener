"""scripts/render_seo_pages.py against a small publish with the real static-data layout."""

import importlib.util
import json
import shutil
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SITE = "https://example.github.io/stock-screener/"


def load_generator():
    spec = importlib.util.spec_from_file_location("render_seo_pages", REPO_ROOT / "scripts" / "render_seo_pages.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def row(symbol, **fields):
    base = {
        "symbol": symbol, "company_name": f"{symbol} Corp", "market": "US", "currency": "USD",
        "is_scannable": True, "data_status": "complete", "current_price": 50.0, "price_change_1d": 1.5,
        "rs_rating": 80.0, "rs_rating_1m": 70.0, "rs_rating_3m": 75.0, "rs_rating_12m": 85.0,
        "stage": 2, "stage_name": "Stage 2 - Advancing", "composite_score": 60.0, "minervini_score": 75.0,
        "canslim_score": 40.0, "ma_alignment": True, "ibd_industry_group": "Semiconductors", "ibd_group_rank": 5,
        "eps_growth_qq": 30.0, "sales_growth_qq": 20.0, "week_52_high_distance": -3.0, "market_cap_usd": 2.5e9,
        "price_sparkline_data": [1.0, 1.02, 0.99, 1.05, 1.08],
    }
    base.update(fields)
    return base


MINERVINI = {
    "id": "minervini", "name": "Minervini Trend Template", "description": "Stage 2 uptrend stocks",
    "sort_by": "minervini_score", "sort_order": "desc",
    "filter_expression": {"group_join": "any", "groups": [], "required": {"match": "all", "conditions": [
        {"field": "minervini_score", "kind": "range", "min": 70, "max": None},
        {"field": "rs_rating", "kind": "range", "min": 70, "max": None},
        {"field": "ma_alignment", "kind": "boolean", "value": True},
        {"field": "stage", "kind": "range", "min": 2, "max": 2},
    ]}},
}
CUP = {
    "id": "se_cup_handle", "name": "Cup with Handle", "description": "Cup-with-handle bases",
    "sort_by": "rs_rating", "sort_order": "desc",
    "filter_expression": {"group_join": "any", "groups": [], "required": {"match": "all", "conditions": [
        {"field": "se_pattern_primary", "kind": "categorical", "values": ["cup_with_handle"]},
    ]}},
}


def write(root, rel, data):
    path = root / "static-data" / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))


@pytest.fixture
def dist(tmp_path):
    gen = "g/1/"
    us_rows = [
        row("AAA", minervini_score=90.0, se_pattern_primary="cup_with_handle"),
        row("BBB", minervini_score=72.0, company_name="<b>Evil & Co</b>"),
        row("CCC", minervini_score=72.0),  # ties BBB; site order breaks ties by symbol
        row("NEW", is_scannable=False, data_status="insufficient_history", rs_rating=None),
        row("BRK/B", minervini_score=10.0),
        # A short-history listing an IPO scan still marks scannable.
        row("IPO", data_status="insufficient_history", rs_rating=None),
    ]
    manifest = {
        "data_root": gen, "generated_at": "2026-10-12T05:25:03Z", "unavailable_markets": ["IN"],
        "markets": {
            "US": {"display_name": "United States", "as_of_date": "2026-10-09", "pages": {
                "scan": {"path": "markets/us/scan/manifest.json"}, "home": {"path": "markets/us/home.json"},
                "groups": {"path": "markets/us/groups.json"}, "breadth": {"path": "markets/us/breadth.json"}}},
            "AU": {"display_name": "Australia", "as_of_date": "2026-10-08", "pages": {
                "scan": {"path": "markets/au/scan/manifest.json"}, "home": {"path": "markets/au/home.json"}}},
        },
    }
    write(tmp_path, "manifest.json", manifest)
    write(tmp_path, gen + "markets/us/scan/manifest.json", {
        "as_of_date": "2026-10-09", "preset_screens": [MINERVINI, CUP], "initial_rows": us_rows[:2],
        "chunks": [{"path": "markets/us/scan/chunks/chunk-0001.json"}],
    })
    write(tmp_path, gen + "markets/us/scan/chunks/chunk-0001.json", {"rows": us_rows})
    write(tmp_path, gen + "markets/us/home.json", {"as_of_date": "2026-10-09", "market_health_exposure": {
        "exposure_score": 45.0, "stance": "Downtrend/Caution", "distribution_day_count": 8}})
    write(tmp_path, gen + "markets/us/groups.json", {"available": True, "rs_as_of_date": "2026-10-08", "payload": {
        "rankings": {"rankings": [
            {"rank": 1, "industry_group": "Semiconductors", "avg_rs_rating": 90.0, "rank_change_1w": 2,
             "rank_change_1m": -1, "rank_change_3m": 5, "num_stocks": 10, "top_symbol": "AAA"},
        ]}}})
    write(tmp_path, gen + "markets/us/breadth.json", {"available": True, "payload": {
        "current": {"date": "2026-10-07", "stocks_up_4pct": 182, "stocks_down_4pct": 116, "t2108_pct": 30.9,
                    "ratio_5day": 0.84, "ratio_10day": 0.85, "new_high_52week_count": 168, "new_low_52week_count": 239},
        "history_90d": [{"date": f"2026-10-0{d}", "stocks_up_4pct": 100 + d, "stocks_down_4pct": 50} for d in range(1, 10)],
    }})
    write(tmp_path, gen + "markets/au/scan/manifest.json", {
        "as_of_date": "2026-10-08", "preset_screens": [MINERVINI],
        "initial_rows": [row("BHP.AX", market="AU", currency="AUD")], "chunks": []})
    write(tmp_path, gen + "markets/au/home.json", {"as_of_date": "2026-10-08"})
    return tmp_path


def render(dist):
    assert load_generator().main(["--dist", str(dist), "--site-url", SITE]) == 0


def page(dist, rel):
    return (dist / rel / "index.html").read_text()


def test_stock_pages_cover_scannable_rows_only(dist):
    render(dist)
    assert (dist / "stocks/us/AAA/index.html").exists()
    assert (dist / "stocks/au/BHP.AX/index.html").exists()
    assert not (dist / "stocks/us/NEW").exists()
    assert not (dist / "stocks/us/IPO").exists()  # incomplete, no RS rating


def test_stock_page_content_is_escaped_canonical_and_linked(dist):
    render(dist)
    html = page(dist, "stocks/us/BBB")
    assert "&lt;b&gt;Evil &amp; Co&lt;/b&gt;" in html and "<b>Evil" not in html
    assert '<link rel="canonical" href="https://example.github.io/stock-screener/stocks/us/BBB/">' in html
    assert "RS rating 80" in html and "As of 2026-10-09" in html
    assert "/stock-screener/screens/us/minervini/" in html  # BBB passes Minervini
    assert "<svg" in html  # price sparkline
    assert "/stock-screener/#/scan?market=US" in html


def test_symbols_are_url_quoted_in_paths(dist):
    render(dist)
    assert (dist / "stocks/us/BRK_2FB/index.html").exists()
    assert "/stock-screener/stocks/us/BRK_2FB/" in page(dist, "stocks/us")
    # Dot-only symbols must not resolve to "." or ".." path segments.
    slug = load_generator().slug
    assert slug(".") == "_2E" and slug("..") == "_2E." and slug("BHP.AX") == "BHP.AX"


def test_screen_page_lists_matches_in_the_sites_order(dist):
    render(dist)
    html = page(dist, "screens/us/minervini")
    assert "3 stocks" in html
    positions = [html.index(f'/stock-screener/stocks/us/{s}/"') for s in ("AAA", "BBB", "CCC")]
    assert positions == sorted(positions)
    # The column the list is sorted by is shown, so the order is explainable.
    assert "<th class=n>Minervini score</th>" in html and "<td class=n>90</td>" in html
    assert '<link rel="icon" href="/stock-screener/fire.svg"' in html
    assert not (dist / "screens/us/canslim").exists()  # preset not in this publish


def test_market_without_breadth_has_no_breadth_page_or_link(dist):
    render(dist)
    assert (dist / "breadth/us/index.html").exists()
    assert not (dist / "breadth/au").exists()
    assert "/stock-screener/breadth/au/" not in page(dist, "markets/au")
    assert "182" in page(dist, "breadth/us")


def test_unavailable_markets_are_skipped(dist):
    render(dist)
    assert not (dist / "markets/in").exists()


def test_sitemap_lists_every_page_with_market_dates(dist):
    render(dist)
    tree = ET.parse(dist / "sitemap.xml")
    ns = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    entries = {u.find("s:loc", ns).text: u.find("s:lastmod", ns).text for u in tree.getroot()}
    assert entries[SITE + "stocks/us/AAA/"] == "2026-10-09"
    assert entries[SITE + "stocks/au/BHP.AX/"] == "2026-10-08"
    assert SITE + "explore/" in entries and SITE + "groups/us/" in entries
    assert SITE + "stocks/us/NEW/" not in entries
    # Sections dated by their own snapshot when it trails the scan.
    assert entries[SITE + "breadth/us/"] == "2026-10-07"
    assert entries[SITE + "groups/us/"] == "2026-10-08"
    assert "(2026-10-07)" in page(dist, "breadth/us") and "(2026-10-08)" in page(dist, "groups/us")
    for url in entries:  # every listed URL was written
        assert (dist / url.removeprefix(SITE) / "index.html").exists()


def test_hub_and_stylesheet(dist):
    render(dist)
    hub = page(dist, "explore")
    assert "/stock-screener/markets/us/" in hub and "/stock-screener/markets/au/" in hub
    assert (dist / "seo.css").exists()


def test_flat_layout_without_data_generations(dist):
    # STATIC_DATA_GENERATIONS=false rollback: no data_root, pages live at the static-data root.
    data = dist / "static-data"
    shutil.copytree(data / "g/1", data, dirs_exist_ok=True)
    manifest = json.loads((data / "manifest.json").read_text())
    del manifest["data_root"]
    (data / "manifest.json").write_text(json.dumps(manifest))
    shutil.rmtree(data / "g")
    render(dist)
    assert (dist / "stocks/us/AAA/index.html").exists()
