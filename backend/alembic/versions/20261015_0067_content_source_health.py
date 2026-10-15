"""Record content source fetch health and a success-only watermark.

Every fetch used to count as a success, so ``last_fetched_at`` served as the
watermark. Existing rows start with ``last_success_at`` equal to it, so their
next poll resumes from the same point.
"""

import sqlalchemy as sa

from alembic import op

revision = "20261015_0067"
down_revision = "20261013_0066"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("content_sources", sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("content_sources", sa.Column(
        "consecutive_failures", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("content_sources", sa.Column("last_error", sa.String(500), nullable=True))
    op.add_column("content_sources", sa.Column("last_error_at", sa.DateTime(timezone=True), nullable=True))
    op.execute("UPDATE content_sources SET last_success_at = last_fetched_at")


def downgrade() -> None:
    for column in ("last_error_at", "last_error", "consecutive_failures", "last_success_at"):
        op.drop_column("content_sources", column)
