"""Resolve scanner RS values from the published Market RS snapshot."""

from __future__ import annotations

from app.domain.relative_strength import BALANCED_RS_FORMULA_VERSION
from app.scanners.base_screener import StockData


class CanonicalStockRsUnavailable(RuntimeError):
    """The canonical formula has no eligible rating for this stock."""


def resolve_stock_rs(stock_data: StockData) -> dict[str, float | int]:
    """Return the stock's canonical ratings.

    Fails closed when no Market RS resolution is attached, or when the symbol
    is absent from its run's single eligible universe, so an ineligible stock
    is never rated against a different, scan-local comparison set.
    """
    source = stock_data.rs_source
    if source is None:
        raise CanonicalStockRsUnavailable(
            f"No Market RS resolution is attached to {stock_data.symbol}"
        )
    if source.ratings is None:
        raise CanonicalStockRsUnavailable(
            f"Canonical RS is unavailable for {stock_data.symbol} in the "
            f"{BALANCED_RS_FORMULA_VERSION} eligible universe"
        )
    return dict(source.ratings)


def stock_rs_lacks_overall(stock_data: StockData) -> bool:
    """True for a young listing: placed horizon ratings, no overall rating."""
    source = stock_data.rs_source
    return source is not None and source.placed_ratings is not None


def resolve_optional_stock_rs(stock_data: StockData) -> dict[str, float | int] | None:
    """Ratings for code that can work without the overall rating.

    None for a young listing. Still raises for a stock with no canonical
    rating at all, so broken data keeps failing closed.
    """
    if stock_rs_lacks_overall(stock_data):
        return None
    return resolve_stock_rs(stock_data)


def canonical_rs_row_fields(stock_data: StockData) -> dict[str, int | None]:
    """The scan row's RS fields; empty when no Market RS resolution is attached."""
    source = stock_data.rs_source
    return source.scan_row_fields() if source is not None else {}
