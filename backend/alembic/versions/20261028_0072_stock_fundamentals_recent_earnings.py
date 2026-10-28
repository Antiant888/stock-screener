"""Keep the reported quarters from the earnings calendar on stock_fundamentals.

Additive. Existing rows start NULL and fill on their next calendar refresh, so
the stock page shows unknown beat/miss counts until then.
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "20261028_0072"
down_revision = "20261027_0071"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("stock_fundamentals", sa.Column(
        "recent_earnings", sa.JSON().with_variant(postgresql.JSONB(), "postgresql"), nullable=True))


def downgrade() -> None:
    op.drop_column("stock_fundamentals", "recent_earnings")
