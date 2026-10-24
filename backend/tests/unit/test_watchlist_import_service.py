from app.services.watchlist_import_service import (
    ImportToken,
    parse_watchlist_import_tokens,
    split_import_results,
)


def _plain_import_symbols(content, format_hint="auto"):
    tokens = parse_watchlist_import_tokens(content, format_hint)
    assert all(token.candidates == (token.text,) for token in tokens)
    return [token.text for token in tokens]


def test_parse_watchlist_import_symbols_handles_text_and_dedupes():
    content = "NVDA, msft\nAAPL\tNVDA\n\n"

    assert _plain_import_symbols(content, format_hint="text") == [
        "NVDA",
        "MSFT",
        "AAPL",
    ]


def test_parse_watchlist_import_symbols_skips_text_headers():
    content = "symbol NVDA msft"

    assert _plain_import_symbols(content, format_hint="text") == [
        "NVDA",
        "MSFT",
    ]


def test_parse_watchlist_import_symbols_skips_csv_headers():
    content = "symbol,name\nNVDA,NVIDIA\nMSFT,Microsoft\n"

    assert _plain_import_symbols(content, format_hint="csv") == [
        "NVDA",
        "MSFT",
    ]


def test_parse_watchlist_import_symbols_handles_tab_delimited_spreadsheet_paste():
    content = "symbol\tname\nAAPL\tApple Inc.\nMSFT\tMicrosoft\n"

    assert _plain_import_symbols(content) == [
        "AAPL",
        "MSFT",
    ]


def test_parse_watchlist_import_symbols_preserves_headerless_symbol_matrix():
    content = "AAPL,MSFT\nNVDA"

    assert _plain_import_symbols(content) == [
        "AAPL",
        "MSFT",
        "NVDA",
    ]


def _texts_and_candidates(content):
    return [(token.text, token.candidates) for token in parse_watchlist_import_tokens(content)]


def test_tradingview_section_names_never_become_symbols():
    content = "###Top AI Picks,NASDAQ:NVDA,###ON Watch,NYSE:ANET"

    assert _texts_and_candidates(content) == [
        ("NASDAQ:NVDA", ("NVDA",)),
        ("NYSE:ANET", ("ANET",)),
    ]


def test_tradingview_export_spanning_lines_keeps_every_row():
    assert _texts_and_candidates("NASDAQ:NVDA,NYSE:ANET\nHKEX:700,TSE:6758") == [
        ("NASDAQ:NVDA", ("NVDA",)),
        ("NYSE:ANET", ("ANET",)),
        ("HKEX:700", ("0700.HK",)),
        ("TSE:6758", ("6758.T",)),
    ]
    assert _texts_and_candidates("###Leaders,NASDAQ:NVDA\r\n###Watch,HKEX:700\n") == [
        ("NASDAQ:NVDA", ("NVDA",)),
        ("HKEX:700", ("0700.HK",)),
    ]


def test_tradingview_unmapped_prefixes_have_no_candidate():
    assert _texts_and_candidates("EURONEXT:AIR,LSE:BP,EURONEXT:MC,TVC:DXY,BINANCE:BTCUSDT") == [
        ("EURONEXT:AIR", ()),
        ("LSE:BP", ()),
        ("EURONEXT:MC", ()),
        ("TVC:DXY", ()),
        ("BINANCE:BTCUSDT", ()),
    ]


def test_tradingview_prefixes_map_to_universe_candidates():
    content = (
        "NYSE:BRK.B,AMEX:SPY,HKEX:5,HKEX:9988,TSE:6758,TWSE:2330,TPEX:3008,"
        "KRX:005930,SSE:600519,SZSE:000001,SGX:D05,ASX:BHP,TSX:SHOP,TSXV:ABC,"
        "XETR:SAP,FWB:SAP,NSE:RELIANCE,BSE:500325,MYX:1155"
    )

    assert _texts_and_candidates(content) == [
        ("NYSE:BRK.B", ("BRK.B", "BRK-B")),
        ("AMEX:SPY", ("SPY",)),
        ("HKEX:5", ("0005.HK",)),
        ("HKEX:9988", ("9988.HK",)),
        ("TSE:6758", ("6758.T",)),
        ("TWSE:2330", ("2330.TW",)),
        ("TPEX:3008", ("3008.TWO",)),
        ("KRX:005930", ("005930.KS", "005930.KQ")),
        ("SSE:600519", ("600519.SS",)),
        ("SZSE:000001", ("000001.SZ",)),
        ("SGX:D05", ("D05.SI",)),
        ("ASX:BHP", ("BHP.AX",)),
        ("TSX:SHOP", ("SHOP.TO",)),
        ("TSXV:ABC", ("ABC.V",)),
        ("XETR:SAP", ("SAP.DE",)),
        ("FWB:SAP", ("SAP.F",)),
        ("NSE:RELIANCE", ("RELIANCE.NS",)),
        ("BSE:500325", ("500325.BO",)),
        ("MYX:1155", ("1155.KL",)),
    ]


def test_split_picks_the_candidate_the_universe_holds():
    tokens = parse_watchlist_import_tokens(
        "KRX:247540,KRX:005930,NYSE:BRK.B,NYSE:BF.B,EURONEXT:AIR,NASDAQ:NVDA,NVDA"
    )

    added, existing, invalid = split_import_results(
        tokens,
        known_symbols={"247540.KQ", "005930.KS", "BRK-B", "BF.B", "AIR", "NVDA"},
        existing_symbols=set(),
    )

    assert added == ["247540.KQ", "005930.KS", "BRK-B", "BF.B", "NVDA"]
    assert existing == []
    assert invalid == ["EURONEXT:AIR"]


def test_split_import_results_classifies_existing_and_invalid_symbols():
    added, existing, invalid = split_import_results(
        [ImportToken(symbol, (symbol,)) for symbol in ["NVDA", "MSFT", "BAD$", "AAPL"]],
        known_symbols={"NVDA", "MSFT", "AAPL"},
        existing_symbols={"AAPL"},
    )

    assert added == ["NVDA", "MSFT"]
    assert existing == ["AAPL"]
    assert invalid == ["BAD$"]
