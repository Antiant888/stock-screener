"""Store young stocks' horizon ratings placed against the eligible universe.

Additive. Completed runs keep their rows and their v2 snapshot contract, and
their placed_symbol_count stays NULL, which means the run predates placement.
The horizon list is a literal on purpose: a migration is frozen in time, and
tests/unit/test_market_rs_schema.py pins it to the domain's RS_HORIZONS.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20261021_0069"
down_revision = "20261021_0068"
branch_labels = None
depends_on = None

HORIZONS = ("1d", "1w", "1m", "3m", "6m", "9m", "12m")


def _placement_check() -> str:
    clauses = []
    for index, horizon in enumerate(HORIZONS):
        rating = f"rs_{horizon}"
        excess = f"excess_return_{horizon}"
        clauses.append(f"({rating} IS NULL OR {rating} BETWEEN 1 AND 99)")
        clauses.append(
            f"(({rating} IS NULL AND {excess} IS NULL) "
            f"OR ({rating} IS NOT NULL AND {excess} IS NOT NULL))"
        )
        if index:
            clauses.append(f"({rating} IS NULL OR rs_{HORIZONS[index - 1]} IS NOT NULL)")
    clauses.append(f"rs_{HORIZONS[-1]} IS NULL")
    return " AND ".join(clauses)


STOCK_RS_PLACEMENT_CHECK = _placement_check()


def upgrade() -> None:
    with op.batch_alter_table("market_rs_runs") as batch_op:
        batch_op.add_column(sa.Column("placed_symbol_count", sa.Integer(), nullable=True))
        batch_op.create_check_constraint(
            "ck_market_rs_run_placed_count",
            "placed_symbol_count IS NULL OR placed_symbol_count >= 0",
        )
    op.create_table(
        "stock_rs_placement_snapshots",
        sa.Column("run_id", sa.Integer(), nullable=False),
        sa.Column("symbol", sa.String(length=20), nullable=False),
        sa.Column("rs_1d", sa.SmallInteger(), nullable=False),
        *(sa.Column(f"rs_{horizon}", sa.SmallInteger(), nullable=True) for horizon in HORIZONS[1:]),
        sa.Column("excess_return_1d", sa.Float(), nullable=False),
        *(
            sa.Column(f"excess_return_{horizon}", sa.Float(), nullable=True)
            for horizon in HORIZONS[1:]
        ),
        sa.CheckConstraint(STOCK_RS_PLACEMENT_CHECK, name="ck_stock_rs_placement_range"),
        sa.ForeignKeyConstraint(["run_id"], ["market_rs_runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("run_id", "symbol"),
    )
    op.create_index(
        "ix_stock_rs_placement_symbol_run",
        "stock_rs_placement_snapshots",
        ["symbol", "run_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_stock_rs_placement_symbol_run", table_name="stock_rs_placement_snapshots")
    op.drop_table("stock_rs_placement_snapshots")
    with op.batch_alter_table("market_rs_runs") as batch_op:
        batch_op.drop_constraint("ck_market_rs_run_placed_count", type_="check")
        batch_op.drop_column("placed_symbol_count")
