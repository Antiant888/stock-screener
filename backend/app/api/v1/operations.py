"""Operations job console endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ...database import get_db
from ...schemas.operations import (
    CotOperationsResponse,
    OperationsCancelJobRequest,
    OperationsCancelJobResponse,
    OperationsJobsResponse,
    OperationsReleaseChainRequest,
)
from ...tasks.market_queues import SHARED_SENTINEL, normalize_market
from ...services.cot_operations_service import CotOperationsService
from ...services.operations_job_service import OperationsJobService
from ...services.social_signal_operations_service import SocialSignalOperationsService

router = APIRouter(prefix="/operations", tags=["operations"])

_service = OperationsJobService()
_social_service = SocialSignalOperationsService()
_cot_service = CotOperationsService()


@router.get("/jobs", response_model=OperationsJobsResponse)
def get_operations_jobs(db: Session = Depends(get_db)) -> OperationsJobsResponse:
    """Return queue, worker, and lease state for the Operations console."""
    return OperationsJobsResponse(**_service.list_jobs(db))


@router.post("/jobs/{task_id}/cancel", response_model=OperationsCancelJobResponse)
def cancel_operations_job(
    task_id: str,
    body: OperationsCancelJobRequest | None = None,
    db: Session = Depends(get_db),
) -> OperationsCancelJobResponse:
    """Safely cancel or revoke a job when the strategy supports it."""
    result = _service.cancel_job(
        db, task_id, expected_strategy=body.expected_strategy if body else None
    )
    if result["status"] == "conflict":
        raise HTTPException(status_code=409, detail=result["message"])
    return OperationsCancelJobResponse(**result)


@router.post("/pipeline-chains/{market}/release", response_model=OperationsCancelJobResponse)
def release_pipeline_chain(
    market: str,
    body: OperationsReleaseChainRequest,
    db: Session = Depends(get_db),
) -> OperationsCancelJobResponse:
    """Release a market's daily pipeline chain claim if ``body.token`` holds it."""
    try:
        market = normalize_market(market)
    except ValueError:
        market = None
    # The pipeline only runs per market; SHARED never holds a chain claim.
    if market is None or market == SHARED_SENTINEL:
        raise HTTPException(status_code=422, detail="Unknown market")
    return OperationsCancelJobResponse(**_service.release_pipeline_chain(db, market, body.token))


@router.get("/social-signals")
def get_social_signal_operations(db: Session = Depends(get_db)) -> dict:
    """Return redacted collection, processing, budget, and backlog health."""
    return _social_service.snapshot(db)


@router.get("/cot", response_model=CotOperationsResponse)
def get_cot_operations(db: Session = Depends(get_db)) -> CotOperationsResponse:
    """Return redacted CFTC import, publication, and price coverage health."""
    return CotOperationsResponse(**_cot_service.snapshot(db))
