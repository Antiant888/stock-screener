"""Store feature rows' table-level details apart from the setup payload.

Every JSON filter and sort over a feature run read ``details_json``: a ``json``
document of ~64 kB per live US row, most of it the setup engine's explain and
candidates payload that only full-detail reads use. PostgreSQL decompresses and
re-parses the whole document per row for each expression, so an unindexed
filter over a 10k-row run took ~19 s (20260925_0057) and a sort ~8-11 s.
``table_details_json`` is the same document without that payload, as ``jsonb``;
those reads take tens of milliseconds on it.

The column is added NULL and not backfilled: ``stock_feature_daily`` keeps every
published run, and filling it here would decompress every historical row during
the deploy. Readers use it only for runs whose rows all carry it
(``SqlFeatureStoreRepository._require_run``); older runs keep reading
``details_json`` and its indexes, which therefore stay. Runs written after this
revision carry both.

The indexes cover the fields 20260617_0021, 20260821_0028 and 20260925_0057
index on ``details_json``, now on the new column, plus the industry group the
peer lookups filter on. Their expressions must match what the repository
compiles; test_feature_store_table_pages_postgres checks that PostgreSQL uses
each one for those statements. They are partial on rows that carry the copy,
so the new column being NULL on every existing row means a build reads only the
narrow heap, never the details, and writes no entries. That lets them be built
without CONCURRENTLY, inside the migration's transaction, which keeps the
revision all-or-nothing with no invalid index left to recover.

Locking: ADD COLUMN takes an ACCESS EXCLUSIVE lock that the migration
transaction holds through the index builds, blocking reads and writes of the
table for those seconds. The backend migrates before it serves, and the
workers wait for it to be healthy, so nothing else is reading the table then.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "20261011_0064"
down_revision = "20261010_0063"
branch_labels = None
depends_on = None

_COLUMN = "table_details_json"

# Range-filtered details keys, compiled as CAST(<column> ->> 'key' AS FLOAT).
_NUMERIC_FIELDS = (
    "rs_rating",
    "minervini_score",
    "canslim_score",
    "stage",
    "eps_growth_qq",
    "volume_breakthrough_score",
    "ipo_score",
    "perf_week",
    "price_change_1d",
    "avg_dollar_volume",
    "ibd_group_rank",
    "resilience_score",
)

_INDEXES = {
    **{
        f"ix_sfd_td_{field}": f"CAST({_COLUMN} ->> '{field}' AS FLOAT)"
        for field in _NUMERIC_FIELDS
    },
    "ix_sfd_td_correction_survivor": f"lower({_COLUMN} ->> 'correction_survivor')",
    "ix_sfd_td_ibd_industry_group": f"({_COLUMN} ->> 'ibd_industry_group')",
}


def _create_indexes() -> None:
    # Partial: B-tree indexes keep NULL keys, so every pre-0064 row would get
    # an entry. PostgreSQL proves the predicate from each strict filter on the
    # column, and only runs whose rows all carry the copy are filtered on it.
    for name, expression in _INDEXES.items():
        op.execute(
            f"CREATE INDEX IF NOT EXISTS {name} "
            f"ON stock_feature_daily (run_id, ({expression})) "
            f"WHERE {_COLUMN} IS NOT NULL"
        )


def _drop_indexes() -> None:
    for name in _INDEXES:
        op.execute(f"DROP INDEX IF EXISTS {name}")


def upgrade() -> None:
    op.add_column(
        "stock_feature_daily",
        sa.Column(
            _COLUMN,
            sa.JSON().with_variant(postgresql.JSONB(), "postgresql"),
            nullable=True,
        ),
    )
    if op.get_bind().dialect.name == "postgresql":
        _create_indexes()


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        _drop_indexes()
    op.drop_column("stock_feature_daily", _COLUMN)
