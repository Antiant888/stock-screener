"""
Stock Universe API endpoints.

Manages the list of scannable stocks from NYSE/NASDAQ.
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
import logging

from ...database import get_db
from ...wiring.bootstrap import get_stock_universe_service

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/refresh")
def refresh_universe(
    exchange: str = None,
    db: Session = Depends(get_db)
):
    """
    Refresh stock universe from finviz.

    Fetches all stocks from NYSE/NASDAQ and updates the database.

    Args:
        exchange: Optional filter (nyse, nasdaq, amex) or None for all
        db: Database session

    Returns:
        Statistics about added/updated/deactivated stocks
    """
    try:
        logger.info(f"Refreshing stock universe (exchange={exchange})")
        stock_universe_service = get_stock_universe_service()

        stats = stock_universe_service.populate_universe(db, exchange)

        logger.info(
            f"Universe refresh completed: {stats['added']} added, "
            f"{stats['updated']} updated, {stats['deactivated']} deactivated, "
            f"{stats['total']} total"
        )

        return {
            "message": "Universe refreshed successfully",
            **stats
        }

    except Exception as e:
        logger.error(f"Error refreshing universe: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail=f"Error refreshing universe: {str(e)}"
        )


@router.get("/stats")
def get_universe_stats(db: Session = Depends(get_db)):
    """
    Get stock universe statistics.

    Returns:
        Total stocks, active stocks, breakdown by exchange
    """
    try:
        stock_universe_service = get_stock_universe_service()
        stats = stock_universe_service.get_stats(db)

        return {
            "total": stats['total'],
            "active": stats['active'],
            "by_exchange": stats['by_exchange'],
            "sp500": stats.get('sp500', 0),
            "by_status": stats.get('by_status', {}),
            "by_market": stats.get('by_market', {}),
            "market_checks": stats.get('market_checks', {}),
            "recent_deactivations": stats.get('recent_deactivations', []),
        }

    except Exception as e:
        logger.error(f"Error getting universe stats: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail=f"Error getting universe stats: {str(e)}"
        )


@router.get("/stats/by-market")
def get_universe_market_audit(db: Session = Depends(get_db)):
    """Get market-level universe audit summary for ops dashboards."""
    try:
        stock_universe_service = get_stock_universe_service()
        return stock_universe_service.get_market_audit(db)
    except Exception as e:
        logger.error(f"Error getting universe market audit: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail=f"Error getting universe market audit: {str(e)}"
        )


@router.post("/symbols")
def add_symbol(
    symbol: str,
    name: str = "",
    is_common_stock: bool | None = None,
    db: Session = Depends(get_db)
):
    """
    Manually add a symbol to the universe.

    Args:
        symbol: Stock symbol
        name: Company name (optional)
        is_common_stock: Optional explicit classification. Omitted values are
            fail-closed for new rows and preserve existing classifications.
        db: Database session

    Returns:
        Success message
    """
    try:
        stock_universe_service = get_stock_universe_service()
        success = stock_universe_service.add_manual_symbol(
            db,
            symbol,
            name,
            is_common_stock=is_common_stock,
        )

        if success:
            return {"message": f"Symbol {symbol} added successfully"}
        else:
            raise HTTPException(
                status_code=400,
                detail=f"Failed to add symbol {symbol}"
            )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error adding symbol: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail=f"Error adding symbol: {str(e)}"
        )


@router.delete("/symbols/{symbol}")
def deactivate_symbol(
    symbol: str,
    db: Session = Depends(get_db)
):
    """
    Deactivate a symbol (remove from scanning).

    Args:
        symbol: Stock symbol
        db: Database session

    Returns:
        Success message
    """
    try:
        stock_universe_service = get_stock_universe_service()
        success = stock_universe_service.deactivate_symbol(db, symbol)

        if success:
            return {"message": f"Symbol {symbol} deactivated successfully"}
        else:
            raise HTTPException(
                status_code=404,
                detail=f"Symbol {symbol} not found"
            )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deactivating symbol: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail=f"Error deactivating symbol: {str(e)}"
        )


@router.post("/refresh-sp500")
def refresh_sp500(db: Session = Depends(get_db)):
    """
    Refresh S&P 500 membership for all stocks.

    Fetches the current S&P 500 list from Wikipedia and updates
    the is_sp500 flag for all stocks in the universe.

    Returns:
        Statistics about S&P 500 update
    """
    try:
        logger.info("Refreshing S&P 500 membership")
        stock_universe_service = get_stock_universe_service()

        stats = stock_universe_service.update_sp500_membership(db)

        logger.info(
            f"S&P 500 refresh completed: {stats['sp500_count']} symbols fetched, "
            f"{stats['updated']} stocks marked as S&P 500"
        )

        return {
            "message": "S&P 500 membership updated successfully",
            **stats
        }

    except Exception as e:
        logger.error(f"Error refreshing S&P 500: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail=f"Error refreshing S&P 500: {str(e)}"
        )
