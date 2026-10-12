import http.client
import urllib.error

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from stockscreen_mcp.data import MANIFEST_TTL_SECONDS, StaticData

from conftest import BASE


def manifest_fetches(fake_publish):
    return sum(url.endswith("/manifest.json") and "markets/" not in url for url in fake_publish.calls)


def test_manifest_is_reused_until_its_ttl_then_refetched(static_data, fake_publish, clock):
    static_data.manifest()
    static_data.manifest()
    assert manifest_fetches(fake_publish) == 1

    clock.now += MANIFEST_TTL_SECONDS + 1
    static_data.manifest()
    assert manifest_fetches(fake_publish) == 2


def test_files_are_cached_per_generation(static_data, fake_publish):
    static_data.load("markets/us/home.json")
    static_data.load("markets/us/home.json")
    assert sum(url.endswith("markets/us/home.json") for url in fake_publish.calls) == 1


def test_scan_rows_merge_initial_rows_and_chunks_by_symbol(static_data):
    scan, rows = static_data.scan("US")

    assert scan["as_of_date"] == "2026-10-09"
    assert [row["symbol"] for row in rows] == ["AAA", "BBB", "CCC", "DDD", "EEE"]
    # The chunk copy of a duplicated symbol wins.
    assert rows[0]["composite_score"] == 81


def test_only_the_most_recent_markets_stay_in_memory(static_data, fake_publish):
    # A large market's rows are hundreds of MB as Python objects; 12 cached
    # markets would not fit on a laptop.
    def chunk_fetches(code):
        return sum(url.endswith(f"markets/{code}/scan/chunks/chunk-0001.json") for url in fake_publish.calls)

    static_data.scan("US")
    static_data.scan("AU")
    static_data.scan("US")  # still cached
    assert chunk_fetches("us") == 1

    static_data.scan("HK")  # evicts AU, the least recently used
    static_data.scan("US")
    assert chunk_fetches("us") == 1
    static_data.scan("AU")
    assert chunk_fetches("au") == 2


def test_scan_chunks_are_not_kept_once_merged(static_data):
    static_data.scan("US")

    assert not [key for key in static_data._cache if "/chunks/" in key[1]]


def test_heavy_fields_are_dropped_on_load(static_data):
    _, rows = static_data.scan("US")

    for row in rows:
        assert "price_sparkline_data" not in row
        assert "rs_sparkline_data" not in row
        assert "field_availability" not in row


def test_a_replaced_generation_is_reloaded_from_the_new_publish(static_data, fake_publish, clock):
    static_data.manifest()  # caches g/1/
    fake_publish.republish("g/2/")  # g/1/ files are gone, manifest still cached

    _, rows = static_data.scan("US")

    assert len(rows) == 5
    assert any(url.startswith(BASE + "g/2/") for url in fake_publish.calls)


def test_a_scan_reads_one_publish_even_if_the_manifest_refreshes_mid_load(static_data, fake_publish, clock):
    # The manifest TTL expires between the scan manifest and its chunks while a new
    # publish is live and the old one is still briefly served.
    old_files = dict(fake_publish.files)

    def expire_and_republish(url):
        if url.endswith("g/1/markets/us/scan/manifest.json"):
            fake_publish.on_fetch = None
            clock.now += MANIFEST_TTL_SECONDS + 1
            fake_publish.republish("g/2/")
            fake_publish.files.update(old_files)

    fake_publish.on_fetch = expire_and_republish
    static_data.scan("US")
    first_load = [u for u in fake_publish.calls if "/chunks/" in u]
    assert first_load == [BASE + "g/1/markets/us/scan/chunks/chunk-0001.json"]

    static_data.scan("US")  # the manifest now names g/2: nothing from g/1 may be reused
    assert [u for u in fake_publish.calls if "/chunks/" in u][-1] == BASE + "g/2/markets/us/scan/chunks/chunk-0001.json"


def test_a_publish_replaced_twice_during_one_load_is_a_tool_error(static_data, fake_publish):
    static_data.manifest()
    generation = iter(range(2, 10))

    def replace_on_every_chunk(url):
        if "/chunks/" in url:
            fake_publish.republish(f"g/{next(generation)}/")

    fake_publish.on_fetch = replace_on_every_chunk
    with pytest.raises(ToolError, match="changed while loading"):
        static_data.scan("US")


@pytest.mark.parametrize("error", [EOFError("truncated gzip"), http.client.IncompleteRead(b"")])
def test_truncated_downloads_become_tool_errors(clock, error):
    def truncated(url):
        raise error

    with pytest.raises(ToolError, match="Could not reach the published data"):
        StaticData(base_url=BASE, fetch=truncated, clock=clock).manifest()


def test_a_missing_file_in_the_current_generation_is_a_tool_error(static_data, fake_publish):
    del fake_publish.files["g/1/markets/us/home.json"]

    with pytest.raises(ToolError, match="markets/us/home.json"):
        static_data.load("markets/us/home.json")


def test_unknown_and_unavailable_markets_are_explained(static_data):
    with pytest.raises(ToolError, match=r"Unknown market 'XX'.*AU, HK, US"):
        static_data.market("xx")
    with pytest.raises(ToolError, match=r"IN.*not available in the current publish"):
        static_data.market("in")
    assert static_data.market("us")["display_name"] == "United States"


def test_network_errors_become_tool_errors(clock):
    def offline(url):
        raise urllib.error.URLError("Name or service not known")

    with pytest.raises(ToolError, match="Could not reach the published data"):
        StaticData(base_url=BASE, fetch=offline, clock=clock).manifest()
