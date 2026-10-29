"""
Fundamentals data management API endpoints.

Provides endpoints for:
- Triggering fundamental data refresh for all stocks
- Viewing fundamentals cache statistics
"""
from fastapi import APIRouter, HTTPException

from ...schemas.common import TaskResponse
from ...schemas.fundamentals import FundamentalsCacheStats
from ...tasks.fundamentals_tasks import refresh_all_fundamentals

router = APIRouter(prefix="/fundamentals", tags=["fundamentals"])


# Endpoints
@router.post("/refresh/all")
def trigger_full_fundamentals_refresh():
    """
    Trigger full refresh of fundamental data for all active stocks.

    This queues a Celery task that will refresh fundamentals for all ~7,000
    active stocks in the universe using finviz.

    Expected duration: ~1 hour at 0.5s rate limit.

    Returns:
        Task information including task_id for tracking progress
    """
    try:
        task = refresh_all_fundamentals.delay()
        message = "Fundamentals refresh queued. Expected duration: ~1 hour for ~7,000 stocks."

        return TaskResponse(
            task_id=task.id,
            message=message,
            status="queued"
        )

    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Error queuing fundamentals refresh: {str(e)}"
        )


@router.get("/stats", response_model=FundamentalsCacheStats)
def get_fundamentals_cache_statistics():
    """
    Get fundamental data cache statistics.

    This endpoint checks a sample of stocks (first 100 active stocks) to
    determine cache health, hit rates, and freshness.

    Returns:
        Cache statistics including:
        - Total stocks checked
        - Redis/DB cache hit counts
        - Fresh vs stale data counts
        - Cache hit rates
        - Last refresh date
    """
    try:
        from ...database import SessionLocal
        from ...models.stock_universe import StockUniverse
        from ...wiring.bootstrap import get_fundamentals_cache, get_provider_snapshot_service
        from datetime import datetime

        db = SessionLocal()

        try:
            # Get sample of active stocks (first 100)
            universe_stocks = db.query(StockUniverse).filter(
                StockUniverse.is_active == True
            ).limit(100).all()

            symbols = [s.symbol for s in universe_stocks]
            cache = get_fundamentals_cache()

            # Collect stats
            total = len(symbols)
            redis_cached = 0
            db_cached = 0
            fresh = 0
            stale = 0

            for symbol in symbols:
                stats = cache.get_cache_stats(symbol)

                if stats['redis_cached']:
                    redis_cached += 1
                if stats['db_cached']:
                    db_cached += 1
                if stats['age_days'] is not None:
                    if stats['age_days'] <= 7:
                        fresh += 1
                    else:
                        stale += 1

            # Get most recent update
            from ...models.stock import StockFundamental
            latest = db.query(StockFundamental).order_by(
                StockFundamental.updated_at.desc()
            ).first()
            provider_snapshot_service = get_provider_snapshot_service()
            snapshot_stats = provider_snapshot_service.get_snapshot_stats(db)

            return FundamentalsCacheStats(
                total_checked=total,
                redis_cached=redis_cached,
                db_cached=db_cached,
                fresh=fresh,
                stale=stale,
                redis_hit_rate=round(redis_cached / total * 100, 1) if total > 0 else 0,
                db_hit_rate=round(db_cached / total * 100, 1) if total > 0 else 0,
                last_refresh_date=latest.updated_at.isoformat() if latest else None,
                published_snapshot_revision=snapshot_stats.get("published_snapshot_revision"),
                published_snapshot_age_days=snapshot_stats.get("published_snapshot_age_days"),
                snapshot_coverage=snapshot_stats.get("snapshot_coverage"),
                recent_single_symbol_fallback_count=cache.get_on_demand_fallback_count(),
                parity_summary=snapshot_stats.get("parity_summary"),
                timestamp=datetime.now().isoformat()
            )

        finally:
            db.close()

    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Error fetching fundamentals cache stats: {str(e)}"
        )
