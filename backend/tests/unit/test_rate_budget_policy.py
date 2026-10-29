"""Unit tests for the per-market rate-budget policy (bead asia.9.2)."""
from __future__ import annotations

import threading
from unittest.mock import MagicMock, patch

import pytest

from app.services.rate_budget_policy import RateBudgetPolicy
from app.tasks.lease_renewal import holding_external_fetch_lease


class TestProviderKey:
    @pytest.mark.parametrize("provider,market,expected", [
        ("yfinance", "US", "yfinance:us"),
        ("yfinance", "HK", "yfinance:hk"),
        ("yfinance", "IN", "yfinance:in"),
        ("yfinance", "JP", "yfinance:jp"),
        ("yfinance", "KR", "yfinance:kr"),
        ("yfinance", "TW", "yfinance:tw"),
        ("yfinance", "CN", "yfinance:cn"),
        ("yfinance", None, "yfinance:shared"),
        ("yfinance", "shared", "yfinance:shared"),
        ("finviz", "HK", "finviz:hk"),
        ("yfinance:batch", "US", "yfinance:batch:us"),
    ])
    def test_provider_key(self, provider, market, expected):
        assert RateBudgetPolicy.provider_key(provider, market) == expected

    def test_unknown_market_raises(self):
        with pytest.raises(ValueError):
            RateBudgetPolicy.provider_key("yfinance", "ZZ")


class TestUniverseWeights:
    def test_equal_split_when_universe_empty(self):
        """Fresh deploy / no universe rows -> equal split across SUPPORTED_MARKETS."""
        with patch("app.services.rate_budget_policy.RateBudgetPolicy._compute_weights_from_db") as m:
            m.return_value = {
                "US": 1 / 7,
                "HK": 1 / 7,
                "IN": 1 / 7,
                "JP": 1 / 7,
                "KR": 1 / 7,
                "TW": 1 / 7,
                "CN": 1 / 7,
            }
            policy = RateBudgetPolicy()
            weights = policy._universe_weights(force_refresh=True)
        assert weights == {
            "US": 1 / 7,
            "HK": 1 / 7,
            "IN": 1 / 7,
            "JP": 1 / 7,
            "KR": 1 / 7,
            "TW": 1 / 7,
            "CN": 1 / 7,
        }
        assert sum(weights.values()) == pytest.approx(1.0)

    def test_universe_weighted_split_normalizes(self):
        with patch("app.services.rate_budget_policy.RateBudgetPolicy._compute_weights_from_db") as m:
            m.return_value = {"US": 0.5, "HK": 0.18, "IN": 0.1, "JP": 0.08, "KR": 0.07, "TW": 0.05, "CN": 0.02}
            policy = RateBudgetPolicy()
            weights = policy._universe_weights(force_refresh=True)
        assert sum(weights.values()) == pytest.approx(1.0)
        assert weights["US"] > weights["HK"] > weights["JP"]
        assert weights["TW"] >= 0.05

    def test_invalidate_cache_forces_recompute(self):
        with patch("app.services.rate_budget_policy.RateBudgetPolicy._compute_weights_from_db") as m:
            m.return_value = {"US": 0.35, "HK": 0.2, "IN": 0.15, "JP": 0.12, "KR": 0.1, "TW": 0.05, "CN": 0.03}
            policy = RateBudgetPolicy()
            policy._universe_weights(force_refresh=True)
            policy._universe_weights()  # cached
            assert m.call_count == 1
            policy.invalidate_weights_cache()
            policy._universe_weights()  # recomputed
            assert m.call_count == 2


class TestRateInterval:
    def test_universe_weighted_default_inversely_scales(self):
        """Bigger universe weight -> smaller interval (gets more tokens/sec)."""
        with patch("app.services.rate_budget_policy.RateBudgetPolicy._compute_weights_from_db") as m:
            m.return_value = {"US": 0.5, "HK": 0.18, "IN": 0.12, "JP": 0.08, "KR": 0.07, "TW": 0.03, "CN": 0.02}
            policy = RateBudgetPolicy()
            # global yfinance interval = 1.0s (yfinance_rate_limit=1 req/s)
            us_interval = policy.get_rate_interval("yfinance", "US")
            hk_interval = policy.get_rate_interval("yfinance", "HK")
        assert us_interval < hk_interval, "US should wait less than HK (bigger universe)"

    def test_per_market_override_wins(self):
        """Setting yfinance_rate_limit_hk overrides the universe-weighted default."""
        with patch("app.services.rate_budget_policy.settings") as mock_settings:
            mock_settings.yfinance_rate_limit = 1
            mock_settings.yfinance_rate_limit_hk = 2.0  # override: 2 req/s = 0.5s interval
            policy = RateBudgetPolicy()
            interval = policy.get_rate_interval("yfinance", "HK")
        assert interval == pytest.approx(0.5)

    def test_shared_market_uses_global_interval(self):
        with patch("app.services.rate_budget_policy.settings") as mock_settings:
            mock_settings.yfinance_rate_limit = 1
            policy = RateBudgetPolicy()
            interval = policy.get_rate_interval("yfinance", None)
        assert interval == pytest.approx(1.0)

    def test_yfinance_batch_in_override_resolves_to_10s(self):
        """YFINANCE_BATCH_RATE_LIMIT_IN=0.1 (req/s) should resolve to a 10s
        interval between IN batch downloads. This is the IN throttle knob the
        static-site workflow relies on to keep Yahoo happy."""
        with patch("app.services.rate_budget_policy.settings") as mock_settings:
            mock_settings.yfinance_batch_rate_limit_interval = 2.0
            mock_settings.yfinance_batch_rate_limit_in = 0.1
            policy = RateBudgetPolicy()
            interval = policy.get_rate_interval("yfinance:batch", "IN")
        assert interval == pytest.approx(10.0)

    def test_yfinance_batch_override_does_not_affect_other_markets(self):
        """The IN override must not leak to US (which gets the universe-weighted
        share of the global ``yfinance_batch_rate_limit_interval``)."""
        with patch("app.services.rate_budget_policy.RateBudgetPolicy._compute_weights_from_db") as m, \
             patch("app.services.rate_budget_policy.settings") as mock_settings:
            m.return_value = {"US": 0.5, "HK": 0.1, "IN": 0.1, "JP": 0.1, "KR": 0.1, "TW": 0.05, "CN": 0.05}
            mock_settings.yfinance_batch_rate_limit_interval = 2.0
            mock_settings.yfinance_batch_rate_limit_in = 0.1
            # Other per-market overrides absent (None)
            mock_settings.yfinance_batch_rate_limit_us = None
            policy = RateBudgetPolicy()
            us_interval = policy.get_rate_interval("yfinance:batch", "US")
            in_interval = policy.get_rate_interval("yfinance:batch", "IN")
        assert in_interval == pytest.approx(10.0)
        assert us_interval == pytest.approx(2.0 / 0.5)  # 4.0s, not 10s


class TestExternalFetchLeaseHolder:
    """Only one market fetches while a task holds ``external_fetch_global``,
    so that task may use the provider's whole interval instead of its
    market's universe-weighted share."""

    WEIGHTS = {"US": 0.5, "HK": 0.1, "IN": 0.1, "JP": 0.1, "KR": 0.1, "TW": 0.05, "CN": 0.05}

    def _interval(self, market="HK", **overrides):
        with patch("app.services.rate_budget_policy.RateBudgetPolicy._compute_weights_from_db") as m, \
             patch("app.services.rate_budget_policy.settings") as mock_settings:
            m.return_value = self.WEIGHTS
            mock_settings.yfinance_batch_rate_limit_interval = 2.0
            mock_settings.yfinance_batch_rate_limit_hk = overrides.get("hk")
            return RateBudgetPolicy().get_rate_interval("yfinance:batch", market)

    def test_holder_gets_the_global_interval(self):
        with holding_external_fetch_lease(threading.Event()):
            assert self._interval() == pytest.approx(2.0)

    def test_without_the_lease_the_market_gets_its_share(self):
        assert self._interval() == pytest.approx(20.0)

    def test_a_lost_lease_falls_back_to_the_share(self):
        lost = threading.Event()
        with holding_external_fetch_lease(lost):
            lost.set()
            assert self._interval() == pytest.approx(20.0)

    def test_per_market_override_still_wins_for_the_holder(self):
        with holding_external_fetch_lease(threading.Event()):
            assert self._interval(hk=0.1) == pytest.approx(10.0)

    def test_holder_gets_the_whole_finviz_interval_too(self):
        with patch("app.services.rate_budget_policy.RateBudgetPolicy._compute_weights_from_db") as m, \
             patch("app.services.rate_budget_policy.settings") as mock_settings:
            m.return_value = self.WEIGHTS
            mock_settings.finviz_rate_limit_interval = 0.5
            mock_settings.finviz_rate_limit_us = None
            policy = RateBudgetPolicy()
            assert policy.get_rate_interval("finviz", "US") == pytest.approx(1.0)
            with holding_external_fetch_lease(threading.Event()):
                assert policy.get_rate_interval("finviz", "US") == pytest.approx(0.5)

    def test_the_lease_ends_with_the_block(self):
        with holding_external_fetch_lease(threading.Event()):
            pass
        assert self._interval() == pytest.approx(20.0)


class TestBatchSize:
    @pytest.mark.parametrize("market,expected_default", [
        # US bumped to 150 in steady-crunching-candle: Yahoo accepts up to
        # MAX_PRICE_BATCH_SIZE (200) and the adaptive shrink halves on
        # transient failure. Non-US markets stay at 50 (smaller universes).
        ("US", 150), ("HK", 50), ("IN", 50), ("JP", 50), ("KR", 50), ("TW", 50), ("CN", 25), ("AU", 50),
    ])
    def test_default_batch_sizes(self, market, expected_default):
        with patch("app.services.rate_budget_policy.settings") as mock_settings:
            # No override: getattr returns None -> use built-in default
            mock_settings.configure_mock(**{f"yfinance_batch_size_{market.lower()}": None})
            policy = RateBudgetPolicy()
            assert policy.get_batch_size("yfinance", market) == expected_default

    def test_override_wins(self):
        with patch("app.services.rate_budget_policy.settings") as mock_settings:
            mock_settings.yfinance_batch_size_hk = 75
            policy = RateBudgetPolicy()
            assert policy.get_batch_size("yfinance", "HK") == 75


class TestBackoffParams:
    def test_default_backoff_per_market(self):
        with patch("app.services.rate_budget_policy.settings") as mock_settings:
            mock_settings.yfinance_backoff_max_s_us = None
            mock_settings.yfinance_backoff_max_s_hk = None
            mock_settings.yfinance_backoff_max_s_au = None
            policy = RateBudgetPolicy()
            us = policy.get_backoff_params("yfinance", "US")
            hk = policy.get_backoff_params("yfinance", "HK")
            au = policy.get_backoff_params("yfinance", "AU")
        assert us["max_s"] == 480
        assert hk["max_s"] == 600  # non-US gets longer cap
        assert au["max_s"] == 600

    def test_au_rate_budget_defaults_match_yfinance_peer_markets(self):
        with patch("app.services.rate_budget_policy.settings") as mock_settings:
            mock_settings.configure_mock(
                yfinance_batch_size_au=None,
                finviz_batch_size_au=None,
                yfinance_workers_au=None,
                finviz_workers_au=None,
                yfinance_backoff_max_s_au=None,
                finviz_backoff_max_s_au=None,
            )
            policy = RateBudgetPolicy(redis_client_factory=lambda: None)
            assert policy.get_batch_size("yfinance", "AU") == 50
            assert policy.get_batch_size("finviz", "AU") == 50
            assert policy.get_provider_workers("yfinance", "AU") == 1
            assert policy.get_provider_workers("finviz", "AU") == 2
            assert policy.get_backoff_params("yfinance", "AU")["max_s"] == 600
            assert policy.get_backoff_params("finviz", "AU")["max_s"] == 480

    def test_us_base_s_preserves_legacy_30_60_120_schedule(self):
        """US base_s=30 keeps the legacy ``30/60/120`` retry schedule used by
        ``BulkDataFetcher._fetch_price_batch_with_retries``. IN bumps to
        base_s=60 so its retries land at 60/120/240."""
        with patch("app.services.rate_budget_policy.settings") as mock_settings:
            mock_settings.yfinance_backoff_max_s_us = None
            mock_settings.yfinance_backoff_max_s_in = None
            policy = RateBudgetPolicy()
            us = policy.get_backoff_params("yfinance", "US")
            in_market = policy.get_backoff_params("yfinance", "IN")
        assert us["base_s"] == 30
        assert in_market["base_s"] == 60

    def test_max_s_override_wins(self):
        with patch("app.services.rate_budget_policy.settings") as mock_settings:
            mock_settings.yfinance_backoff_max_s_hk = 900
            policy = RateBudgetPolicy()
            params = policy.get_backoff_params("yfinance", "HK")
        assert params["max_s"] == 900
        # Other defaults still present
        assert "base_s" in params and "factor" in params


class TestThrottleCounters:
    def _policy_with_redis(self, mock_client):
        return RateBudgetPolicy(redis_client_factory=lambda: mock_client)

    def test_record_429_increments_running_and_daily(self):
        mock_client = MagicMock()
        pipe = MagicMock()
        mock_client.pipeline.return_value = pipe
        policy = self._policy_with_redis(mock_client)

        policy.record_429("yfinance", "HK")

        pipe.incr.assert_any_call("ratelimit:429:yfinance:hk")
        # Daily-bucketed key has YYYYMMDD suffix; just assert it was incremented
        daily_calls = [c for c in pipe.incr.call_args_list if "ratelimit:429:yfinance:hk:" in c.args[0]]
        assert len(daily_calls) == 1
        pipe.expire.assert_called_once()
        pipe.execute.assert_called_once()

    def test_record_throttle_wait_skips_zero(self):
        mock_client = MagicMock()
        policy = self._policy_with_redis(mock_client)
        policy.record_throttle_wait("yfinance", "US", wait_s=0.0)
        mock_client.pipeline.assert_not_called()

    def test_record_throttle_wait_increments_count_and_seconds(self):
        mock_client = MagicMock()
        pipe = MagicMock()
        mock_client.pipeline.return_value = pipe
        policy = self._policy_with_redis(mock_client)

        policy.record_throttle_wait("yfinance", "JP", wait_s=2.5)

        pipe.incr.assert_called_once()
        pipe.incrbyfloat.assert_called_once()
        # incrbyfloat called with (key, 2.5)
        assert pipe.incrbyfloat.call_args.args[1] == pytest.approx(2.5)

    def test_records_silently_when_redis_unavailable(self):
        policy = RateBudgetPolicy(redis_client_factory=lambda: None)
        # Should not raise
        policy.record_429("yfinance", "US")
        policy.record_throttle_wait("yfinance", "US", wait_s=1.0)
