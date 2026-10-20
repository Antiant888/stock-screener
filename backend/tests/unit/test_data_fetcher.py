from app.services.data_fetcher import DataFetcher
from app.services.yfinance_service import YFinanceService


def test_data_fetcher_supports_explicit_dependency_injection():
    yfinance_service = object()

    fetcher = DataFetcher(yfinance_service=yfinance_service)

    assert fetcher._yfinance_service is yfinance_service


def test_data_fetcher_constructs_without_runtime_container(monkeypatch):
    expected_yfinance = YFinanceService()
    monkeypatch.setattr(
        DataFetcher,
        "_build_default_yfinance_service",
        staticmethod(lambda: expected_yfinance),
    )
    fetcher = DataFetcher()

    assert fetcher._yfinance_service is expected_yfinance
