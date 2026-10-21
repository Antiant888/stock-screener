"""Drop the per-market RS formula pointer table.

Nothing reads a pointer any more: balanced-horizon-percentile-v2 is the only
RS formula. Upgrade refuses while a pointer names another formula for a
market this release would stop serving: one with a latest_published_market
Feature pointer, or one whose ibd_group_ranks rows carry that pointer's
formula, since every reader now filters group ranks on the balanced formula.
Revision 0025 seeds a legacy pointer for every catalog market and tags
existing group ranks legacy, so an empty database upgrading in one run has
neither and passes. Downgrade recreates the table and seeds a balanced row
for every market with a latest_published_market Feature pointer, so the
previous release serves the same markets.
"""

import sqlalchemy as sa

from alembic import op

revision = "20261021_0068"
down_revision = "20261015_0067"
branch_labels = None
depends_on = None

BALANCED_FORMULA_VERSION = "balanced-horizon-percentile-v2"
PUBLISHED_POINTER_PREFIX = "latest_published_market:"


def upgrade() -> None:
    rows = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT p.market, p.formula_version, "
                "EXISTS (SELECT 1 FROM feature_run_pointers f "
                "WHERE f.key = 'latest_published_market:' || p.market), "
                "EXISTS (SELECT 1 FROM ibd_group_ranks g "
                "WHERE g.market = p.market AND g.rs_formula_version = p.formula_version) "
                "FROM market_rs_formula_pointers p "
                "WHERE p.formula_version <> :formula "
                "ORDER BY p.market"
            ).bindparams(formula=BALANCED_FORMULA_VERSION)
        )
        .all()
    )
    stale = []
    for market, formula, published, ranked in rows:
        reasons = [
            reason
            for reason, hit in (
                ("published Feature run", published),
                ("legacy group ranks", ranked),
            )
            if hit
        ]
        if reasons:
            stale.append(f"{market}={formula!r} ({', '.join(reasons)})")
    if stale:
        listed = ", ".join(stale)
        raise RuntimeError(
            f"Markets served under a retired RS formula: {listed}. "
            f"This release serves only {BALANCED_FORMULA_VERSION!r} and cannot "
            "serve those markets. Finish the balanced rollout on the previous "
            "release first. For each listed market, run 'python -m "
            "app.scripts.backfill_market_rs --market <MARKET> --through-date "
            "<YYYY-MM-DD> --static-staging-dir <DIR> --activate' on that release, "
            "where <DIR> is a new, empty absolute directory for each Market that "
            "nothing serves, such as /var/tmp/stockscreen-rs-<market>-<date>. "
            "Then upgrade. See 'Balanced Market RS Publication' in "
            "docs/OPERATIONS.md. If a listed market is no longer enabled, delete "
            "its row from market_rs_formula_pointers."
        )
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
            "SELECT substr(key, :prefix_length + 1), :formula "
            "FROM feature_run_pointers WHERE substr(key, 1, :prefix_length) = :prefix"
        ).bindparams(
            formula=BALANCED_FORMULA_VERSION,
            prefix=PUBLISHED_POINTER_PREFIX,
            prefix_length=len(PUBLISHED_POINTER_PREFIX),
        )
    )
