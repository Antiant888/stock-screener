"""Resolve group constituent stocks from the published Feature run."""

from __future__ import annotations

import logging
from typing import Callable

from sqlalchemy import desc
from sqlalchemy.orm import Session

from app.domain.relative_strength import RsPublicationIdentity
from app.domain.scanning.models import ScanResultItemDomain
from app.infra.db.models.feature_store import FeatureRun
from app.infra.db.repositories.feature_store_repo import SqlFeatureStoreRepository
from app.services.feature_run_rs_identity import feature_run_matches_rs_source

logger = logging.getLogger(__name__)

FeatureRepoFactory = Callable[[Session], SqlFeatureStoreRepository]


class GroupConstituentPublicationUnavailable(LookupError):
    """No Feature run matches the exact canonical Group publication."""


class GroupConstituentSource:
    """Read group constituents while keeping source-selection out of services."""

    def __init__(
        self,
        *,
        feature_repo_factory: FeatureRepoFactory = SqlFeatureStoreRepository,
    ) -> None:
        self._feature_repo_factory = feature_repo_factory

    def get_constituent_items(
        self,
        db: Session,
        industry_group: str,
        *,
        publication: RsPublicationIdentity,
    ) -> tuple[ScanResultItemDomain, ...]:
        feature_run = self._get_feature_run_for_publication(
            db,
            publication=publication,
        )
        if feature_run is not None:
            peers = self._feature_repo_factory(db).get_peers_by_industry_for_run(
                feature_run.id,
                industry_group,
                include_sparklines=True,
            )
            logger.info(
                "Found %d feature-run stocks for group %s (%s)",
                len(peers),
                industry_group,
                publication.snapshot.market,
            )
            return peers

        raise GroupConstituentPublicationUnavailable(
            "No published Feature run matches canonical Group publication "
            f"{publication.snapshot.market}/"
            f"{publication.snapshot.as_of_date.isoformat()}/"
            f"run-{publication.market_rs_run_id}."
        )

    def _get_feature_run_for_publication(
        self,
        db: Session,
        *,
        publication: RsPublicationIdentity,
    ) -> FeatureRun | None:
        query = (
            db.query(FeatureRun)
            .filter(
                FeatureRun.status == "published",
                FeatureRun.as_of_date == publication.snapshot.as_of_date,
            )
            .order_by(
                desc(FeatureRun.published_at),
                desc(FeatureRun.id),
            )
        )
        for run in query.all():
            if feature_run_matches_rs_source(
                run,
                identity=publication.snapshot,
                market_rs_run_id=publication.market_rs_run_id,
                universe_size=publication.universe_size,
            ):
                return run
        return None
