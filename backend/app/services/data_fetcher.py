"""
Data fetching service that reads technicals and industry data from yfinance.
Handles caching; yfinance_service handles rate limiting.
"""
from typing import TYPE_CHECKING, Optional, Dict, Any
from datetime import datetime, timedelta
import logging
from sqlalchemy.orm import Session

from ..models.stock import StockTechnical, StockIndustry, StockPrice
from ..config import settings

if TYPE_CHECKING:
    from .yfinance_service import YFinanceService

logger = logging.getLogger(__name__)


class DataFetcher:
    """Fetches technicals and industry classification, cached in the database."""

    def __init__(
        self,
        db: Optional[Session] = None,
        *,
        yfinance_service: "YFinanceService | None" = None,
    ):
        """
        Initialize data fetcher.

        Args:
            db: Database session for caching (optional)
            yfinance_service: Optional injected yfinance service dependency.
        """
        self.db = db
        self._yfinance_service = yfinance_service or self._build_default_yfinance_service()

    @staticmethod
    def _build_default_yfinance_service() -> "YFinanceService":
        # Standalone construction keeps DataFetcher usable in ad-hoc scripts/tests
        # without requiring runtime container initialization.
        from .yfinance_service import YFinanceService

        return YFinanceService()

    def get_stock_technicals(
        self,
        symbol: str,
        force_refresh: bool = False
    ) -> Optional[Dict[str, Any]]:
        """
        Get stock technical indicators.

        Args:
            symbol: Stock ticker symbol
            force_refresh: Force data refresh

        Returns:
            Dict with technical data or None if error
        """
        # Check cache
        if self.db and not force_refresh:
            cached = self._get_cached_technicals(symbol)
            if cached:
                logger.debug(f"Using cached technicals for {symbol}")
                return cached

        # Fetch from yfinance (rate limiting handled inside yfinance_service)
        logger.info(f"Calculating technicals for {symbol}")

        # Get price range
        price_range = self._yfinance_service.get_price_range(symbol)
        if not price_range:
            return None

        # Get moving averages
        mas = self._yfinance_service.calculate_moving_averages(symbol)
        if not mas:
            return None

        # Get volume data
        volume = self._yfinance_service.get_volume_data(symbol)

        # Combine data
        data = {
            "symbol": symbol,
            "current_price": mas.get("current_price"),
            "ma_50": mas.get("ma_50"),
            "ma_150": mas.get("ma_150"),
            "ma_200": mas.get("ma_200"),
            "ma_200_month_ago": mas.get("ma_200_month_ago"),
            "high_52w": price_range.get("high_52w"),
            "low_52w": price_range.get("low_52w"),
            "avg_volume_50d": volume.get("avg_volume_50d") if volume else None,
            "current_volume": volume.get("current_volume") if volume else None,
        }

        # Cache the result
        if self.db:
            self._cache_technicals(symbol, data)

        return data

    def get_industry_classification(self, symbol: str) -> Optional[Dict[str, str]]:
        """
        Get industry classification for a stock.

        Args:
            symbol: Stock ticker symbol

        Returns:
            Dict with sector and industry info
        """
        # Check cache
        if self.db:
            cached = self._get_cached_industry(symbol)
            if cached:
                return cached

        # Fetch from yfinance (rate limiting handled inside yfinance_service)
        info = self._yfinance_service.get_stock_info(symbol)

        if not info:
            return None

        classification = {
            "symbol": symbol,
            "sector": info.get("sector", "Unknown"),
            "industry": info.get("industry", "Unknown"),
        }

        # Cache the result
        if self.db:
            self._cache_industry(symbol, classification)

        return classification

    # Cache helper methods

    def _get_cached_technicals(self, symbol: str) -> Optional[Dict[str, Any]]:
        """Get cached technical data."""
        try:
            cached = self.db.query(StockTechnical).filter(
                StockTechnical.symbol == symbol
            ).first()

            if not cached:
                return None

            # Check if cache is stale (1 day for technicals)
            cache_age = datetime.now() - cached.updated_at.replace(tzinfo=None)
            if cache_age > timedelta(hours=settings.cache_ttl_hours):
                return None

            return {
                "symbol": cached.symbol,
                "current_price": cached.current_price,
                "ma_50": cached.ma_50,
                "ma_150": cached.ma_150,
                "ma_200": cached.ma_200,
                "ma_200_month_ago": cached.ma_200_month_ago,
                "high_52w": cached.high_52w,
                "low_52w": cached.low_52w,
                "avg_volume_50d": cached.avg_volume_50d,
                "current_volume": cached.current_volume,
                "rs_rating": cached.rs_rating,
                "stage": cached.stage,
                "vcp_score": cached.vcp_score,
            }

        except Exception as e:
            logger.error(f"Error reading technical cache for {symbol}: {e}")
            return None

    def _cache_technicals(self, symbol: str, data: Dict[str, Any]):
        """Cache technical data."""
        try:
            technical = self.db.query(StockTechnical).filter(
                StockTechnical.symbol == symbol
            ).first()

            if technical:
                for key, value in data.items():
                    if hasattr(technical, key):
                        setattr(technical, key, value)
            else:
                technical = StockTechnical(**data)
                self.db.add(technical)

            self.db.commit()
            logger.debug(f"Cached technicals for {symbol}")

        except Exception as e:
            logger.error(f"Error caching technicals for {symbol}: {e}")
            self.db.rollback()

    def _get_cached_industry(self, symbol: str) -> Optional[Dict[str, str]]:
        """Get cached industry classification."""
        try:
            cached = self.db.query(StockIndustry).filter(
                StockIndustry.symbol == symbol
            ).first()

            if not cached:
                return None

            return {
                "symbol": cached.symbol,
                "sector": cached.sector,
                "industry": cached.industry,
            }

        except Exception as e:
            logger.error(f"Error reading industry cache for {symbol}: {e}")
            return None

    def _cache_industry(self, symbol: str, data: Dict[str, str]):
        """Cache industry classification."""
        try:
            industry = self.db.query(StockIndustry).filter(
                StockIndustry.symbol == symbol
            ).first()

            if industry:
                for key, value in data.items():
                    if hasattr(industry, key):
                        setattr(industry, key, value)
            else:
                industry = StockIndustry(**data)
                self.db.add(industry)

            self.db.commit()
            logger.debug(f"Cached industry for {symbol}")

        except Exception as e:
            logger.error(f"Error caching industry for {symbol}: {e}")
            self.db.rollback()
