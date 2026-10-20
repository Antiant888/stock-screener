"""Drop the per-market RS formula pointer table.

Nothing reads a pointer any more: balanced-horizon-percentile-v2 is the only
RS formula. Downgrade recreates the table and seeds a balanced row for every
market that has Market RS runs, which is the state every deployment was in.
"""

import sqlalchemy as sa

from alembic import op

revision = "20261021_0068"
down_revision = "20261015_0067"
branch_labels = None
depends_on = None

BALANCED_FORMULA_VERSION = "balanced-horizon-percentile-v2"


def upgrade() -> None:
    op.drop_table("market_rs_formula_pointers")


def downgrade() -> None:
    op.create_table(
        "market_rs_formula_pointers",
        sa.Column("market", sa.String(length=8), nullable=False),
        sa.Column("formula_version", sa.String(length=64), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("market"),
    )
    op.execute(
        sa.text(
            "INSERT INTO market_rs_formula_pointers (market, formula_version) "
            "SELECT DISTINCT market, :formula FROM market_rs_runs"
        ).bindparams(formula=BALANCED_FORMULA_VERSION)
    )
