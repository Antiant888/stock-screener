"""Close scheduled-task runs that no worker signal reported as finished."""

from __future__ import annotations

from ..celery_app import celery_app
from ..database import SessionLocal


@celery_app.task(name="app.tasks.task_history_tasks.reconcile_task_runs")
def reconcile_task_runs() -> dict:
    from ..services.task_registry_service import TaskRegistryService

    with SessionLocal() as db:
        return {"closed": TaskRegistryService().reconcile_open_runs(db)}
