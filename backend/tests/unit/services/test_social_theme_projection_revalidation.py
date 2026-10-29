"""Saved rows that no longer revalidate: pinned inputs refuse, historical mentions are skipped."""
from datetime import timedelta

import pytest

from tests.unit.repositories.test_social_signal_writer import (
    NOW, batch, link, saved_work, store, unquoted_excerpt, writer,
)


def pinned_run(store, run_id="run"):
    w = writer(store)
    w.create_run(run_id, NOW)
    first = w.persist_observations(batch(), run_id=run_id)
    second = w.persist_observations(batch(2, post_id="200"), run_id=run_id)
    content_ids = dict(w.read_run_inputs(run_id).content_ids)
    with store.begin() as db:
        works = (saved_work(db, content_ids["100"], first.posts[0]),
                 saved_work(db, content_ids["200"], second.posts[0]))
        for work in works:
            link(db, run_id, work)
        ids = tuple(work.id for work in works)
    return w, ids


def test_prepare_refuses_a_pinned_result_that_no_longer_revalidates_with_a_stable_reason(store):
    from app.services.social_theme_projection_service import SocialThemeProjectionService

    _, (bad_id, _) = pinned_run(store)
    unquoted_excerpt(store, bad_id)

    with store() as db:
        with pytest.raises(ValueError, match="^social_pinned_input_invalid$"):
            SocialThemeProjectionService(db).prepare("run", NOW)


def test_prepare_still_reports_a_pinned_row_awaiting_analysis_as_incomplete(store):
    from app.infra.db.models.social_analysis import SocialExtractionWork
    from app.services.social_theme_projection_service import SocialThemeProjectionService

    _, (waiting_id, _) = pinned_run(store)
    with store.begin() as db:
        db.get(SocialExtractionWork, waiting_id).state = "waiting_budget"

    with store() as db:
        with pytest.raises(ValueError, match="^social_work_incomplete$"):
            SocialThemeProjectionService(db).prepare("run", NOW)


def test_prepare_application_skips_a_mentioned_result_that_no_longer_revalidates(store):
    from app.models.theme import ThemeCluster, ThemeMention
    from app.services.social_theme_projection_service import SocialThemeProjectionService

    w, pinned = pinned_run(store)
    w.create_run("old", NOW - timedelta(days=1))
    earlier = batch(age=-24)
    earlier = w.persist_observations(earlier, run_id="old")
    content_id = dict(w.read_run_inputs("old").content_ids)["100"]
    with store.begin() as db:
        mentioned = saved_work(db, content_id, earlier.posts[0], model="older-model")
        theme = ThemeCluster(name="Cooling", canonical_key="cooling", display_name="Cooling",
                             pipeline="technical", lifecycle_state="active", is_active=True)
        db.add(theme)
        db.flush()
        db.add(ThemeMention(social_work_id=mentioned.id, theme_cluster_id=theme.id,
                            source_type="twitter", raw_theme="cooling", pipeline="technical"))
        mentioned_id = mentioned.id
    unquoted_excerpt(store, mentioned_id)

    with store() as db:
        service = SocialThemeProjectionService(db)
        projection = service.prepare("run", NOW)
        application = service.prepare_application(projection, theme_keys=("cooling",))

    assert tuple(work_id for work_id, _, _ in application.decoded_work) == pinned
