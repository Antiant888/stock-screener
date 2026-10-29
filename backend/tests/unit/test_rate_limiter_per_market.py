"""Per-market rate-limiter integration (bead asia.9.2).

Verifies that ``RedisRateLimiter.wait_for_market`` resolves the right key
via RateBudgetPolicy and records throttle telemetry when it actually slept.
"""
from __future__ import annotations

import contextvars
import threading
from types import SimpleNamespace
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
            mock_policy.get_rate_interval.side_effect = lambda p, m: 5.0 if m else 2.0
            get_policy.return_value = mock_policy

            limiter = RedisRateLimiter()
            with patch.object(limiter, "wait_all", return_value=0.0) as inner:
                limiter.wait_for_market("yfinance", "HK")

            inner.assert_called_once()
            assert inner.call_args.args[0] == {"yfinance": 2.0, "yfinance:hk": 5.0}
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
            with patch.object(limiter, "wait_all", return_value=0.5):
                limiter.wait_for_market("yfinance", "US")

            mock_policy.record_throttle_wait.assert_called_once_with("yfinance", "US", 0.5)

    @patch("app.services.rate_limiter.settings")
    def test_no_throttle_recording_when_zero_wait(self, mock_settings):
        mock_settings.redis_enabled = False
        with patch("app.services.rate_budget_policy.get_rate_budget_policy") as get_policy:
            mock_policy = MagicMock()
            mock_policy.provider_key.return_value = "yfinance:us"
            mock_policy.get_rate_interval.return_value = 0.0
            get_policy.return_value = mock_policy

            limiter = RedisRateLimiter()
            with patch.object(limiter, "wait_all", return_value=0.0):
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
            real_policy.get_rate_interval.side_effect = lambda p, m: 5.0 if m else 2.0
            get_policy.return_value = real_policy

            limiter = RedisRateLimiter()
            keys_seen = []
            with patch.object(limiter, "wait_all", side_effect=lambda keys, **kw: keys_seen.extend(keys) or 0.0):
                limiter.wait_for_market("yfinance", "US")
                limiter.wait_for_market("yfinance", "HK")

        assert keys_seen == ["yfinance", "yfinance:us", "yfinance", "yfinance:hk"]


class TestProviderBudget:
    """Fallback limiter on a fake clock. ``others`` reserve the provider-wide
    key the way concurrent workers would: their sleeps run in parallel, so
    they do not advance this caller's clock."""

    WEIGHTS = {"US": 0.5, "HK": 0.1, "IN": 0.1, "JP": 0.1, "KR": 0.1, "TW": 0.05, "CN": 0.05}

    @pytest.fixture
    def env(self, monkeypatch):
        from app.services.rate_budget_policy import RateBudgetPolicy

        clock = [1000.0]
        parallel = [False]

        def sleep(seconds):
            if not parallel[0]:
                clock[0] += seconds

        monkeypatch.setattr("app.services.rate_limiter.time.monotonic", lambda: clock[0])
        monkeypatch.setattr("app.services.rate_limiter.time.sleep", sleep)
        policy = RateBudgetPolicy(redis_client_factory=lambda: None)
        policy._compute_weights_from_db = lambda: self.WEIGHTS
        limiter = RedisRateLimiter(jitter_ms=0)
        limiter._get_redis_client = lambda: None

        def others(count=0, market=None, timeout_s=120.0):
            parallel[0] = True
            try:
                for _ in range(count):
                    limiter.wait("yfinance:batch", min_interval_s=2.0)
                if market is not None:
                    contextvars.Context().run(
                        limiter.wait_for_market, "yfinance:batch", market, timeout_s
                    )
            finally:
                parallel[0] = False

        with patch("app.services.rate_budget_policy.get_rate_budget_policy", return_value=policy), \
             patch("app.services.rate_budget_policy.settings") as mock_settings:
            mock_settings.yfinance_batch_rate_limit_interval = 2.0
            mock_settings.yfinance_batch_rate_limit_hk = None
            mock_settings.yfinance_batch_rate_limit_us = None
            mock_settings.yfinance_batch_rate_limit_in = 0.1
            yield SimpleNamespace(clock=clock, limiter=limiter, others=others)

    def test_lease_holder_and_other_markets_share_the_global_budget(self, env):
        from app.tasks.lease_renewal import holding_external_fetch_lease

        for _ in range(5):
            with holding_external_fetch_lease(threading.Event()):
                env.limiter.wait_for_market("yfinance:batch", "HK")
            env.limiter.wait_for_market("yfinance:batch", "US")

        assert env.clock[0] - 1000.0 == pytest.approx(18.0)

    def test_market_spacing_holds_when_the_global_key_is_busy(self, env):
        env.others(6)
        env.limiter.wait_for_market("yfinance:batch", "IN")
        first = env.clock[0]
        env.limiter.wait_for_market("yfinance:batch", "IN")

        assert env.clock[0] - first == pytest.approx(10.0)

    def test_timeout_covers_both_keys(self, env):
        from app.services.rate_limiter import RateLimitTimeoutError
        from app.tasks.lease_renewal import holding_external_fetch_lease

        with holding_external_fetch_lease(threading.Event()):
            env.limiter.wait_for_market("yfinance:batch", "HK")
            env.others(1)
            with pytest.raises(RateLimitTimeoutError):
                env.limiter.wait_for_market("yfinance:batch", "HK", timeout_s=3.0)

    def test_same_market_call_outside_the_lease_does_not_stall_the_holder(self, env):
        from app.tasks.lease_renewal import holding_external_fetch_lease

        with holding_external_fetch_lease(threading.Event()):
            env.limiter.wait_for_market("yfinance:batch", "HK")
            first = env.clock[0]
            env.others(market="HK")
            env.limiter.wait_for_market("yfinance:batch", "HK")

        assert env.clock[0] - first == pytest.approx(4.0)

    def test_a_market_still_inside_its_share_does_not_hold_provider_slots(self, env):
        from app.services.rate_limiter import RateLimitTimeoutError
        from app.tasks.lease_renewal import holding_external_fetch_lease

        with holding_external_fetch_lease(threading.Event()):
            env.limiter.wait_for_market("yfinance:batch", "US")
            first = env.clock[0]
            env.others(market="HK")
            with pytest.raises(RateLimitTimeoutError):
                env.others(market="HK", timeout_s=1.0)
            env.limiter.wait_for_market("yfinance:batch", "US")

        assert env.clock[0] - first == pytest.approx(4.0)
