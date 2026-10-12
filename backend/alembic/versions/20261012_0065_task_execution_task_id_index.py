"""Index task_execution_history.task_id: worker task signals look runs up by it."""

from alembic import op

revision = "20261012_0065"
down_revision = "20261011_0064"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index("idx_task_execution_task_id", "task_execution_history", ["task_id"])


def downgrade() -> None:
    op.drop_index("idx_task_execution_task_id", table_name="task_execution_history")
