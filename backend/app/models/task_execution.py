"""Task execution history model for tracking Celery task runs"""
from sqlalchemy import Column, Integer, String, Float, DateTime, JSON, Text, Index
from sqlalchemy.sql import func
from ..database import Base


class TaskExecutionHistory(Base):
    """Track execution history of scheduled Celery tasks"""

    __tablename__ = "task_execution_history"

    id = Column(Integer, primary_key=True, index=True)
    task_name = Column(String(100), nullable=False, index=True)  # e.g., "weekly-full-refresh"
    task_function = Column(String(200))  # e.g., "app.tasks.cache_tasks.weekly_full_refresh"
    task_id = Column(String(100), nullable=True)  # Celery task ID

    # Execution details
    status = Column(String(20), default="queued")  # queued, running, completed, failed, cancelled
    started_at = Column(DateTime(timezone=True), server_default=func.now())
    completed_at = Column(DateTime(timezone=True))
    duration_seconds = Column(Float)

    # Result
    result_summary = Column(JSON)  # Store task return value summary
    error_message = Column(Text)

    # Trigger source
    # manual, auto (Beat or another dispatch), or the task's own origin kwarg
    triggered_by = Column(String(20), default="manual")

    __table_args__ = (
        Index("idx_task_name_started", "task_name", "started_at"),
        Index("idx_task_execution_task_id", "task_id"),
    )
