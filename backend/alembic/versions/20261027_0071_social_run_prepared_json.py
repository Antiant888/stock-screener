"""Move a staged social run's prepared publication payload to its own column.

``application_progress_json`` carried the ``prepared`` payload (about 6 MB per
staged run) next to the small collection state that every generation scan
reads. On the live database that column had grown to 581 MB over 528 rows,
and the social worker was OOM-killed inside ``_latest_resumable_run`` while
loading every run. ``prepared_json`` is mapped deferred, so those scans no
longer load it and only code that opens one run's publication does.

The backfill splits existing rows with PostgreSQL JSON operators. The column
type is ``json``, so each expression casts through ``jsonb`` and back.
"""

import sqlalchemy as sa

from alembic import op

revision = "20261027_0071"
down_revision = "20261024_0070"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("social_signal_runs", sa.Column("prepared_json", sa.JSON(), nullable=True))
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            """
            UPDATE social_signal_runs
               SET prepared_json = (application_progress_json::jsonb -> 'prepared')::json,
                   application_progress_json = (application_progress_json::jsonb - 'prepared')::json
             WHERE application_progress_json::jsonb -> 'prepared' IS NOT NULL
            """
        )


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            """
            UPDATE social_signal_runs
               SET application_progress_json = (
                       application_progress_json::jsonb
                       || jsonb_build_object('prepared', prepared_json::jsonb)
                   )::json
             WHERE prepared_json IS NOT NULL
            """
        )
    op.drop_column("social_signal_runs", "prepared_json")
