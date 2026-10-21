from dataclasses import dataclass
from datetime import date

from sqlalchemy.orm import Session

from app.domain.feature_store.run_metadata import feature_run_market
from app.domain.relative_strength import (
    BALANCED_RS_FORMULA_VERSION,
    GroupSnapshotIdentity,
    RsPublicationIdentity,
    balanced_run_has_current_snapshot_contract,
)
from app.infra.db.models.feature_store import FeatureRun, FeatureRunPointer
from app.infra.db.models.relative_strength import MarketRsRun


class FeatureRunRsIdentityError(RuntimeError):
    """Feature metadata cannot identify one coherent RS snapshot."""


@dataclass(frozen=True)
class FeatureRunRsIdentityResolution:
    publication: RsPublicationIdentity

    @property
    def identity(self) -> GroupSnapshotIdentity:
        return self.publication.snapshot

    @property
    def market_rs_run_id(self) -> int:
        return self.publication.market_rs_run_id

    @property
    def universe_size(self) -> int:
        return self.publication.universe_size


def resolve_feature_run_rs_identity(
    feature_run,
    *,
    ranking_date: date,
) -> FeatureRunRsIdentityResolution:
    if feature_run is None:
        raise FeatureRunRsIdentityError("Feature run does not exist")
    config = dict(
        getattr(feature_run, "config_json", None)
        or getattr(feature_run, "config", None)
        or {}
    )
    market = feature_run_market(feature_run) or config.get("market")
    if not str(market or "").strip():
        raise FeatureRunRsIdentityError("Feature run has no market identity")
    formula = str(config.get("rs_formula_version") or "").strip()
    canonical_values = (
        config.get("market_rs_run_id"),
        config.get("rs_as_of_date"),
        config.get("rs_universe_size"),
    )
    if not formula:
        if all(value is None for value in canonical_values):
            raise FeatureRunRsIdentityError("Feature run has no RS identity metadata")
        raise FeatureRunRsIdentityError(
            "Feature run has partial canonical RS metadata without rs_formula_version"
        )
    if formula != BALANCED_RS_FORMULA_VERSION:
        raise FeatureRunRsIdentityError(
            f"Feature run RS formula {formula!r} is not {BALANCED_RS_FORMULA_VERSION!r}"
        )
    configured_as_of = config.get("rs_as_of_date")
    if configured_as_of is not None and str(configured_as_of) != ranking_date.isoformat():
        raise FeatureRunRsIdentityError(
            f"Feature run RS date {configured_as_of} does not match "
            f"ranking date {ranking_date.isoformat()}"
        )
    if any(value is None for value in canonical_values):
        raise FeatureRunRsIdentityError("Feature run has partial canonical RS metadata")
    try:
        publication = RsPublicationIdentity(
            snapshot=GroupSnapshotIdentity(
                market,
                ranking_date,
                BALANCED_RS_FORMULA_VERSION,
            ),
            market_rs_run_id=int(config["market_rs_run_id"]),
            universe_size=int(config["rs_universe_size"]),
        )
    except (TypeError, ValueError) as exc:
        raise FeatureRunRsIdentityError(
            f"Feature run has invalid canonical RS metadata: {exc}"
        ) from exc
    return FeatureRunRsIdentityResolution(publication=publication)


def feature_run_matches_rs_source(
    feature_run,
    *,
    identity: GroupSnapshotIdentity,
    market_rs_run_id: int | None,
    universe_size: int | None = None,
) -> bool:
    try:
        resolved = resolve_feature_run_rs_identity(
            feature_run,
            ranking_date=identity.as_of_date,
        )
    except FeatureRunRsIdentityError:
        return False
    if resolved.identity != identity:
        return False
    if resolved.market_rs_run_id != market_rs_run_id:
        return False
    return universe_size is None or resolved.universe_size == universe_size


def resolve_published_balanced_publication(
    db: Session,
    market: str,
) -> FeatureRunRsIdentityResolution | None:
    """The market's committed balanced publication, or None.

    Follows ``latest_published_market:<MARKET>`` to a published Feature run,
    resolves its balanced RS identity for that market, and requires the Market RS run that
    identity names to be completed, current-contract, and for the same market,
    date and universe size. A completed run alone is not a publication: the
    shadow backfill writes those before validation and the pointer move.
    """
    market = str(market).strip().upper()
    pointer = db.get(FeatureRunPointer, f"latest_published_market:{market}")
    feature_run = db.get(FeatureRun, pointer.run_id) if pointer is not None else None
    if feature_run is None or feature_run.status != "published":
        return None
    try:
        resolution = resolve_feature_run_rs_identity(
            feature_run,
            ranking_date=feature_run.as_of_date,
        )
    except FeatureRunRsIdentityError:
        return None
    if resolution.identity.market != market:
        return None
    rs_run = db.get(MarketRsRun, resolution.market_rs_run_id)
    if (
        rs_run is None
        or rs_run.status != "completed"
        or rs_run.formula_version != BALANCED_RS_FORMULA_VERSION
        or not balanced_run_has_current_snapshot_contract(rs_run)
        or rs_run.market != resolution.identity.market
        or rs_run.as_of_date != resolution.identity.as_of_date
        or rs_run.eligible_symbol_count != resolution.universe_size
    ):
        return None
    return resolution
