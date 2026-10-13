"""Store the hottest feature-row sort/filter fields as typed columns.

After 20261011_0064, filters and sorts over a run read ``table_details_json``,
a ~3.5 kB ``jsonb`` copy per row, and still decompress it for every row of the
run: on a 10,050-row US run a sort by an unindexed field took ~150 ms, an
unindexed filter plus sort ~320 ms, and filter options ~770 ms. The fields the
scan page sorts and filters on most, and the industry and sector the peer and
filter-option queries read, now also live in typed columns of the same name
(``HOT_NUMERIC_FIELDS``/``HOT_TEXT_FIELDS`` in the model), which reads them
without touching the copy.

As in 0064, the columns are added NULL and not backfilled, because
``stock_feature_daily`` keeps every published run. ``hot_fields_stored`` marks
rows written with them, since NULL is also a legitimate value of each field;
``SqlFeatureStoreRepository._require_run`` reads the columns only for runs
whose rows all carry the marker, and older runs keep reading the JSON copy or
``details_json`` with their indexes.

The indexes cover the promoted fields 0064 indexed, plus industry group and
sector for the peer lookups. Each is partial on its column being NOT NULL: no
existing row qualifies, so each build scans the heap but writes no entries, and
PostgreSQL proves the predicate from the strict range, IN and equality filters
the repository emits. 0064's indexes on the copy for those same fields are
dropped (see ``_SUPERSEDED_INDEXES``).

Locking: as in 0064, ADD COLUMN's ACCESS EXCLUSIVE lock is held through the
index builds and drops, blocking the table for one heap scan per index; the
backend migrates before it serves.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20261013_0066"
down_revision = "20261012_0065"
branch_labels = None
depends_on = None

_NUMERIC_COLUMNS = (
    "rs_rating",
    "rs_rating_1m",
    "rs_rating_3m",
    "rs_rating_12m",
    "minervini_score",
    "canslim_score",
    "adr_percent",
    "eps_growth_qq",
    "sales_growth_qq",
    "beta",
    "perf_week",
    "perf_month",
    "perf_3m",
    "perf_6m",
    "avg_dollar_volume",
)
_TEXT_COLUMNS = ("ibd_industry_group", "gics_sector")

_INDEXED_COLUMNS = (
    "rs_rating",
    "minervini_score",
    "canslim_score",
    "eps_growth_qq",
    "perf_week",
    "avg_dollar_volume",
    "ibd_industry_group",
    "gics_sector",
)
_INDEXES = {f"ix_sfd_hot_{column}": column for column in _INDEXED_COLUMNS}

# 0064's indexes on the copy for the same fields. Every new row still has the
# copy, so they would take an entry per row and serve no new run; only runs
# written between 0064 and this revision used them, and those now scan.
_SUPERSEDED_INDEXES = {
    **{
        f"ix_sfd_td_{column}": f"CAST(table_details_json ->> '{column}' AS FLOAT)"
        for column in _INDEXED_COLUMNS
        if column in _NUMERIC_COLUMNS
    },
    "ix_sfd_td_ibd_industry_group": "(table_details_json ->> 'ibd_industry_group')",
}


def _create_indexes() -> None:
    for name, column in _INDEXES.items():
        op.execute(
            f"CREATE INDEX IF NOT EXISTS {name} "
            f"ON stock_feature_daily (run_id, {column}) "
            f"WHERE {column} IS NOT NULL"
        )


def _drop_indexes() -> None:
    for name in _INDEXES:
        op.execute(f"DROP INDEX IF EXISTS {name}")


def _drop_superseded_indexes() -> None:
    for name in _SUPERSEDED_INDEXES:
        op.execute(f"DROP INDEX IF EXISTS {name}")


def _restore_superseded_indexes() -> None:
    # As 0064 built them; rows written since then are indexed again.
    for name, expression in _SUPERSEDED_INDEXES.items():
        op.execute(
            f"CREATE INDEX IF NOT EXISTS {name} "
            f"ON stock_feature_daily (run_id, ({expression})) "
            f"WHERE table_details_json IS NOT NULL"
        )


def upgrade() -> None:
    op.add_column(
        "stock_feature_daily", sa.Column("hot_fields_stored", sa.Boolean(), nullable=True)
    )
    for column in _NUMERIC_COLUMNS:
        op.add_column("stock_feature_daily", sa.Column(column, sa.Float(), nullable=True))
    for column in _TEXT_COLUMNS:
        op.add_column("stock_feature_daily", sa.Column(column, sa.Text(), nullable=True))
    if op.get_bind().dialect.name == "postgresql":
        _create_indexes()
        _drop_superseded_indexes()


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        _drop_indexes()
        _restore_superseded_indexes()
    for column in (*_TEXT_COLUMNS, *_NUMERIC_COLUMNS, "hot_fields_stored"):
        op.drop_column("stock_feature_daily", column)
