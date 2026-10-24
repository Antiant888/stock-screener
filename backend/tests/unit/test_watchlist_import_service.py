from app.services.watchlist_import_service import (
    parse_watchlist_import_symbols,
    split_import_results,
)


def test_parse_watchlist_import_symbols_handles_text_and_dedupes():
    content = "NVDA, msft\nAAPL\tNVDA\n\n"

    assert parse_watchlist_import_symbols(content, format_hint="text") == [
        "NVDA",
        "MSFT",
        "AAPL",
    ]


def test_parse_watchlist_import_symbols_skips_text_headers():
    content = "symbol NVDA msft"

    assert parse_watchlist_import_symbols(content, format_hint="text") == [
        "NVDA",
        "MSFT",
    ]


def test_parse_watchlist_import_symbols_skips_csv_headers():
    content = "symbol,name\nNVDA,NVIDIA\nMSFT,Microsoft\n"

    assert parse_watchlist_import_symbols(content, format_hint="csv") == [
        "NVDA",
        "MSFT",
    ]


def test_parse_watchlist_import_symbols_handles_tab_delimited_spreadsheet_paste():
    content = "symbol\tname\nAAPL\tApple Inc.\nMSFT\tMicrosoft\n"

    assert parse_watchlist_import_symbols(content) == [
        "AAPL",
        "MSFT",
    ]


def test_parse_watchlist_import_symbols_preserves_headerless_symbol_matrix():
    content = "AAPL,MSFT\nNVDA"

    assert parse_watchlist_import_symbols(content) == [
        "AAPL",
        "MSFT",
        "NVDA",
    ]


def test_parse_watchlist_import_symbols_reads_tradingview_export():
    content = "###Leaders,NASDAQ:NVDA,NYSE:ANET,###Watch,HKEX:700\n"

    assert parse_watchlist_import_symbols(content) == ["NVDA", "ANET", "0700.HK"]


def test_parse_watchlist_import_symbols_maps_tradingview_prefixes_to_repo_symbols():
    content = (
        "NYSE:BRK.B,AMEX:SPY,HKEX:5,HKEX:9988,TSE:6758,TWSE:2330,TPEX:3008,"
        "KRX:005930,SSE:600519,SZSE:000001,SGX:D05,ASX:BHP,TSX:SHOP,TSXV:ABC,"
        "XETR:SAP,NSE:RELIANCE,BSE:500325,MYX:1155,BINANCE:BTCUSDT"
    )

    assert parse_watchlist_import_symbols(content) == [
        "BRK.B",
        "SPY",
        "0005.HK",
        "9988.HK",
        "6758.T",
        "2330.TW",
        "3008.TWO",
        "005930.KS",
        "600519.SS",
        "000001.SZ",
        "D05.SI",
        "BHP.AX",
        "SHOP.TO",
        "ABC.V",
        "SAP.DE",
        "RELIANCE.NS",
        "500325.BO",
        "1155.KL",
        "BTCUSDT",
    ]


def test_split_import_results_classifies_existing_and_invalid_symbols():
    added, existing, invalid = split_import_results(
        ["NVDA", "MSFT", "BAD$", "AAPL"],
        known_symbols={"NVDA", "MSFT", "AAPL"},
        existing_symbols={"AAPL"},
    )

    assert added == ["NVDA", "MSFT"]
    assert existing == ["AAPL"]
    assert invalid == ["BAD$"]
