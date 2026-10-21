"""Background-market runtime bootstrap completion tests."""

from __future__ import annotations

import pytest


def test_background_completion_marks_market_failure_without_global_state(
    monkeypatch,
):
    from app.services.bootstrap_readiness_service import (
        BootstrapReadiness,
        MarketBootstrapReadiness,
    )
    from app.tasks import runtime_bootstrap_tasks as module

    class _FakeSession:
        def close(self):
            pass

    class _FakeReadinessService:
        def evaluate(
            self, db, *, enabled_markets, bootstrap_started_at=None,
            balanced_publication_markets=(),
        ):
            calls["evaluate"] = (
                db, enabled_markets, bootstrap_started_at, balanced_publication_markets
            )
            return BootstrapReadiness(
                empty_system=False,
                market_results={
                    "HK": MarketBootstrapReadiness(
                        market="HK",
                        core_ready=True,
                        scan_ready=False,
                    ),
                },
            )

    session = _FakeSession()
    calls = {}
    completions = []

    monkeypatch.setattr(module, "SessionLocal", lambda: session)
    monkeypatch.setattr(
        module, "_is_current_bootstrap_dispatch", lambda *_args, **_kwargs: True
    )
    monkeypatch.setattr(
        module,
        "_finish_bootstrap_market",
        lambda *_args, **kwargs: completions.append(kwargs["completion"]) or True,
    )
    monkeypatch.setattr(
        "app.services.bootstrap_readiness_service.BootstrapReadinessService",
        _FakeReadinessService,
    )
    monkeypatch.setattr(
        "app.services.runtime_preferences_service.get_runtime_preferences",
        lambda _db: type("Prefs", (), {"bootstrap_started_at": "started-at"})(),
    )
    monkeypatch.setattr(
        "app.services.runtime_preferences_service.set_bootstrap_state",
        lambda *_args, **_kwargs: pytest.fail(
            "background completion must not mutate global bootstrap state"
        ),
    )

    result = module.complete_background_market_bootstrap.run(
        market="HK", dispatch_id="dispatch-current"
    )

    assert calls["evaluate"] == (session, ["HK"], "started-at", ())
    assert result == {
        "status": "failed",
        "market": "HK",
        "reason": "missing published auto scan",
    }
    assert len(completions) == 1
    assert completions[0].primary is False
    assert completions[0].failure_stage_key == "scan"
    assert completions[0].failure_message == "Bootstrap scan did not publish"
