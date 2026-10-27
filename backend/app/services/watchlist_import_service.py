"""Helpers for parsing pasted watchlist imports."""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

from app.domain.markets.symbol_suffixes import market_symbol_suffix_registry

from .symbol_format import SYMBOL_SHAPE_RE

# TradingView ``EXCHANGE:TICKER`` prefix -> (market, venue alias) pairs to
# try, best first. Suffixes come from the market suffix registry; a ``None``
# alias takes the market's default. KRX covers both KOSPI (.KS) and KOSDAQ
# (.KQ). A prefix missing here gives no candidate, so ``EURONEXT:AIR`` is
# reported, never read as AIR.
_TRADINGVIEW_PREFIX_VENUES: dict[str, tuple[tuple[str, str | None], ...]] = {
    "NASDAQ": (("US", "NASDAQ"),),
    "NYSE": (("US", "NYSE"),),
    "AMEX": (("US", "AMEX"),),
    "NYSEARCA": (("US", None),),
    "BATS": (("US", None),),
    "CBOE": (("US", None),),
    "OTC": (("US", None),),
    "HKEX": (("HK", "HKEX"),),
    "TSE": (("JP", "TSE"),),
    "TWSE": (("TW", "TWSE"),),
    "TPEX": (("TW", "TPEX"),),
    "KRX": (("KR", "KOSPI"), ("KR", "KOSDAQ")),
    "SSE": (("CN", "SSE"),),
    "SZSE": (("CN", "SZSE"),),
    "SGX": (("SG", "SGX"),),
    "ASX": (("AU", "ASX"),),
    "MYX": (("MY", "MYX"),),
    "TSX": (("CA", "TSX"),),
    "TSXV": (("CA", "TSXV"),),
    "XETR": (("DE", "XETR"),),
    "FWB": (("DE", "FWB"),),
    "NSE": (("IN", "NSE"),),
    "BSE": (("IN", "BSE"),),
}
_TRADINGVIEW_PREFIX_SUFFIXES = {
    prefix: tuple(
        market_symbol_suffix_registry.suffix_for(market, alias) or ""
        for market, alias in venues
    )
    for prefix, venues in _TRADINGVIEW_PREFIX_VENUES.items()
}
# A slash marks a URL such as https://x, never a ticker.
_TRADINGVIEW_SYMBOL_RE = re.compile(r"^[A-Z][A-Z0-9_]*:[^\s/]+$")
_TEXT_DELIMITERS = ",;\t"
_TABLE_DELIMITERS = ",;"
# A spreadsheet cell that is itself a comma or semicolon list: no whitespace.
_LIST_CELL_RE = re.compile(r"^[^\s,;]+(?:[,;][^\s,;]+)+$")


@dataclass(frozen=True)
class ImportToken:
    """One imported token and the universe symbols it may name, best first."""

    text: str
    candidates: tuple[str, ...]


_HEADER_TOKENS = {
    "symbol",
    "symbols",
    "ticker",
    "tickers",
    "code",
}
# Heuristic requires at least one A-Z letter so numeric-only cells
# (price/volume columns) don't masquerade as symbols.
_SYMBOL_HEURISTIC_PATTERN = re.compile(r"^(?=.*[A-Z])[A-Z0-9][A-Z0-9.\-]{0,19}$")


def parse_watchlist_import_tokens(
    content: str,
    format_hint: str | None = "auto",
) -> list[ImportToken]:
    """Parse pasted watchlist content into de-duplicated uppercase tokens."""

    normalized_hint = (format_hint or "auto").lower()
    stripped = content.strip()
    if not stripped:
        return []

    if normalized_hint not in {"auto", "text", "csv"}:
        raise ValueError(f"Unsupported import format: {format_hint}")

    if normalized_hint == "text":
        rows = _tokenize_rows(stripped, _TEXT_DELIMITERS)
        raw_tokens = [word for row in rows for cell in row for word in cell.split()]
    else:
        raw_tokens = _parse_tabular_symbols(stripped)
    tokens = [_import_token(token.strip().upper()) for token in raw_tokens]

    deduped: list[ImportToken] = []
    seen: set[str] = set()
    for token in tokens:
        if not token.text or token.text.lower() in _HEADER_TOKENS or token.text in seen:
            continue
        seen.add(token.text)
        deduped.append(token)
    return deduped


def _import_token(cell: str) -> ImportToken:
    prefix, separator, ticker = cell.partition(":")
    if not separator:
        ticker, suffixes = cell, ("",)
    else:
        suffixes = _TRADINGVIEW_PREFIX_SUFFIXES.get(prefix, ())
        if prefix == "HKEX" and ticker.isdigit():
            ticker = f"{int(ticker):04d}"
    # Class shares are often written BRK.B; the universe may hold BRK-B.
    tickers = dict.fromkeys((ticker, ticker.replace(".", "-")))
    return ImportToken(cell, tuple(t + suffix for t in tickers for suffix in suffixes))


def _looks_like_symbol_token(token: str) -> bool:
    # Heuristic parser should reject numeric-only cells (price/volume columns).
    return bool(_SYMBOL_HEURISTIC_PATTERN.match(token.strip().upper()))


def _tokenize_rows(content: str, delimiters: str | None) -> list[list[str]]:
    """Split content into rows of stripped cells; the only tokenizer here.

    A cell that opens with a double quote runs to the closing quote, as in
    CSV: delimiters and newlines inside it are kept and ``""`` is one quote,
    so "Apple, Inc." stays one cell. A TradingView ``###Section`` cell comes
    back empty, keeping its column so a header row still lines up.

    ``delimiters=None`` picks them per line: a line holding a tab is a
    spreadsheet row whose names may hold commas, so tabs alone split it;
    other lines split on commas and semicolons, mixed freely. In a tab line,
    an unquoted cell such as ``NVDA,MSFT`` (no spaces, every piece a symbol)
    is still a symbol list and splits; a name such as ``Apple, Inc.`` does not.
    """

    # \r\n and a bare \r (classic Mac files) each end a row, like \n.
    content = content.replace("\r\n", "\n").replace("\r", "\n")
    rows: list[list[str]] = []
    row: list[str] = []
    cell: list[str] = []
    in_quotes = False
    quoted = False
    index = 0
    row_delimiters = _line_delimiters(content, 0, delimiters)
    while index < len(content):
        char = content[index]
        index += 1
        if in_quotes:
            if char != '"':
                cell.append(char)
            elif content[index:index + 1] == '"':
                cell.append('"')
                index += 1
            else:
                in_quotes = False
        elif char == '"' and not "".join(cell).strip():
            in_quotes = quoted = True
            cell = []
        elif char in row_delimiters:
            row.extend(_finish_cell(cell, quoted, row_delimiters))
            cell, quoted = [], False
        elif char == "\n":
            row.extend(_finish_cell(cell, quoted, row_delimiters))
            rows.append(row)
            row, cell, quoted = [], [], False
            row_delimiters = _line_delimiters(content, index, delimiters)
        else:
            cell.append(char)
    if in_quotes:
        # An unclosed quote would swallow the rest of the paste; read the
        # quote as a plain character instead.
        return _tokenize_rows(content.replace('"', ""), delimiters)
    row.extend(_finish_cell(cell, quoted, row_delimiters))
    rows.append(row)
    return [row for row in rows if any(row)]


def _line_delimiters(content: str, start: int, delimiters: str | None) -> str:
    if delimiters is not None:
        return delimiters
    end = content.find("\n", start)
    line = content[start:] if end < 0 else content[start:end]
    return "\t" if "\t" in line else _TABLE_DELIMITERS


def _finish_cell(chars: list[str], quoted: bool, row_delimiters: str) -> list[str]:
    cell = "".join(chars).strip()
    if cell.startswith("###"):
        return [""]
    if not quoted and row_delimiters == "\t" and _LIST_CELL_RE.match(cell):
        pieces = re.split(r"[,;]", cell)
        if all(_is_symbol_cell([piece]) for piece in pieces):
            return pieces
    return [cell]


def _parse_tabular_symbols(content: str) -> list[str]:
    rows = _tokenize_rows(content, None)
    if not rows:
        return []
    if len(rows) == 1:
        # One line is a list, never a table, and a header word in it is
        # dropped as a token.
        return [word for cell in rows[0] for word in cell.split()]

    symbol_column = next(
        (index for index, cell in enumerate(rows[0]) if cell.lower() in _HEADER_TOKENS),
        None,
    )
    if symbol_column is not None:
        return [
            row[symbol_column]
            for row in rows[1:]
            if symbol_column < len(row) and row[symbol_column]
        ]

    # Header-less: every cell is a symbol unless some cell of a multi-cell row
    # is not (a name, a note or a link), which makes the paste a table read by
    # its first column. A line with no delimiter is a list of words either way.
    is_matrix = all(
        _is_symbol_cell(cell.split())
        for row in rows
        if len([cell for cell in row if cell]) > 1
        for cell in row
        if cell
    )
    symbols: list[str] = []
    for row in rows:
        cells = [cell for cell in row if cell]
        if is_matrix or len(cells) == 1:
            symbols.extend(word for cell in cells for word in cell.split())
        else:
            symbols.append(cells[0])
    return symbols


def _is_symbol_cell(words: list[str]) -> bool:
    # Several words make a symbol cell only when each is EXCHANGE:TICKER, so a
    # name such as "NVIDIA Corp" keeps a header-less paste a table.
    if all(_TRADINGVIEW_SYMBOL_RE.match(word.upper()) for word in words):
        return True
    return len(words) == 1 and _looks_like_symbol_token(words[0])


def split_import_results(
    tokens: Iterable[ImportToken],
    known_symbols: set[str],
    existing_symbols: set[str],
) -> tuple[list[str], list[str], list[str]]:
    """Classify tokens into addable, existing, and invalid groups.

    Each token resolves to its first candidate in ``known_symbols`` (else its
    first one already in the watchlist). Unresolved tokens are reported under
    their original text.
    """

    added_candidates: list[str] = []
    skipped_existing: list[str] = []
    invalid_symbols: list[str] = []
    resolved_symbols: set[str] = set()

    for token in tokens:
        symbol = next(
            (c for c in token.candidates if c in known_symbols), None
        ) or next((c for c in token.candidates if c in existing_symbols), None)
        if symbol in resolved_symbols:
            continue
        if symbol is not None:
            resolved_symbols.add(symbol)
        if symbol in existing_symbols:
            skipped_existing.append(symbol)
        elif symbol is None or not SYMBOL_SHAPE_RE.match(symbol):
            invalid_symbols.append(token.text)
        else:
            added_candidates.append(symbol)

    return added_candidates, skipped_existing, invalid_symbols
