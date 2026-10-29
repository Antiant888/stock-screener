"""Per-market rate-limiter integration (bead asia.9.2).

Verifies that ``RedisRateLimiter.wait_for_market`` resolves the right key
via RateBudgetPolicy and records throttle telemetry when it actually slept.
"""
from __future__ import annotations

import threading
from unittest.mock import MagicMock, patch

import pytest

from app.services.rate_limiter import RedisRateLimiter


class TestWaitForMarket:
    @patch("app.services.rate_limiter.settings")
    def test_resolves_per_market_key_and_interval(self, mock_settings):
        mock_settings.redis_enabled = False  # force fallback path
        with patch("app.services.rate_budget_policy.get_rate_budget_policy") as get_policy:
            mock_policy = MagicMock()
            mock_policy.provider_key.return_value = "yfinance:hk"
            mock_policy.get_rate_interval.return_value = 0.0  # no wait
            get_policy.return_value = mock_policy

            limiter = RedisRateLimiter()
            with patch.object(limiter, "wait", return_value=0.0) as inner:
                limiter.wait_for_market("yfinance", "HK")

            assert [c.args[0] for c in inner.call_args_list] == ["yfinance:hk", "yfinance"]
            assert inner.call_args_list[0].kwargs["min_interval_s"] == 0.0
            mock_policy.provider_key.assert_called_with("yfinance", "HK")

    @patch("app.services.rate_limiter.settings")
    def test_records_throttle_when_actually_waited(self, mock_settings):
        mock_settings.redis_enabled = False
        with patch("app.services.rate_budget_policy.get_rate_budget_policy") as get_policy:
            mock_policy = MagicMock()
            mock_policy.provider_key.return_value = "yfinance:us"
            mock_policy.get_rate_interval.return_value = 1.0
            get_policy.return_value = mock_policy

            limiter = RedisRateLimiter()
            with patch.object(limiter, "wait", side_effect=[0.5, 0.25]):
                limiter.wait_for_market("yfinance", "US")

            mock_policy.record_throttle_wait.assert_called_once_with("yfinance", "US", 0.75)

    @patch("app.services.rate_limiter.settings")
    def test_no_throttle_recording_when_zero_wait(self, mock_settings):
        mock_settings.redis_enabled = False
        with patch("app.services.rate_budget_policy.get_rate_budget_policy") as get_policy:
            mock_policy = MagicMock()
            mock_policy.provider_key.return_value = "yfinance:us"
            mock_policy.get_rate_interval.return_value = 0.0
            get_policy.return_value = mock_policy

            limiter = RedisRateLimiter()
            with patch.object(limiter, "wait", return_value=0.0):
                limiter.wait_for_market("yfinance", "US")

            mock_policy.record_throttle_wait.assert_not_called()


class TestWaitKeyIsolation:
    """Two markets calling wait_for_market hit different share keys and one
    provider-wide key."""

    @patch("app.services.rate_limiter.settings")
    def test_us_and_hk_use_different_keys(self, mock_settings):
        mock_settings.redis_enabled = False
        with patch("app.services.rate_budget_policy.get_rate_budget_policy") as get_policy:
            real_policy = MagicMock()
            # Simulate real provider_key behavior
            real_policy.provider_key.side_effect = lambda p, m: f"{p}:{m.lower()}" if m else f"{p}:shared"
            real_policy.get_rate_interval.return_value = 0.0
            get_policy.return_value = real_policy

            limiter = RedisRateLimiter()
            keys_seen = []
            with patch.object(limiter, "wait", side_effect=lambda key, **kw: keys_seen.append(key) or 0.0):
                limiter.wait_for_market("yfinance", "US")
                limiter.wait_for_market("yfinance", "HK")

        assert keys_seen == ["yfinance:us", "yfinance", "yfinance:hk", "yfinance"]


class TestProviderBudget:
    def test_lease_holder_and_other_markets_share_the_global_budget(self, monkeypatch):
        from app.services.rate_budget_policy import RateBudgetPolicy
        from app.tasks.lease_renewal import holding_external_fetch_lease

        clock = [1000.0]
        monkeypatch.setattr("app.services.rate_limiter.time.monotonic", lambda: clock[0])
        monkeypatch.setattr(
            "app.services.rate_limiter.time.sleep",
            lambda seconds: clock.__setitem__(0, clock[0] + seconds),
        )
        policy = RateBudgetPolicy(redis_client_factory=lambda: None)
        policy._compute_weights_from_db = lambda: {
            "US": 0.5, "HK": 0.1, "IN": 0.1, "JP": 0.1, "KR": 0.1, "TW": 0.05, "CN": 0.05,
        }
        limiter = RedisRateLimiter(jitter_ms=0)
        limiter._get_redis_client = lambda: None

        with patch("app.services.rate_budget_policy.get_rate_budget_policy", return_value=policy), \
             patch("app.services.rate_budget_policy.settings") as mock_settings:
            mock_settings.yfinance_batch_rate_limit_interval = 2.0
            mock_settings.yfinance_batch_rate_limit_hk = None
            mock_settings.yfinance_batch_rate_limit_us = None
            for _ in range(5):
                with holding_external_fetch_lease(threading.Event()):
                    limiter.wait_for_market("yfinance:batch", "HK")
                limiter.wait_for_market("yfinance:batch", "US")

        assert clock[0] - 1000.0 == pytest.approx(18.0)
