"""Let a watchlist opt in to the daily per-market scan.

Additive. Existing watchlists start unflagged, so no daily scan runs until a
user turns one on.
"""

import sqlalchemy as sa

from alembic import op

revision = "20261024_0070"
down_revision = "20261021_0069"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("user_watchlists", sa.Column(
        "include_in_daily_scan", sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade() -> None:
    op.drop_column("user_watchlists", "include_in_daily_scan")
