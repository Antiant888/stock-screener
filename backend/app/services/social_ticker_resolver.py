"""Read-only listing resolution.

Company identity comes from an administrator attestation or, for a common stock
with no sign of a second listing, from the listing itself. Signs of a second
listing (a depositary-receipt name, a name repeated within the market such as
share classes, HK while mainland listings are loaded) and fund names still need
attestation.
Double counting needs both listings counted, so excluding one side is enough.
"""
import re
from collections.abc import Mapping
from types import MappingProxyType

from sqlalchemy import func

from app.domain.social_signals.records import SUPPORTED_MARKETS, TickerResolution
from app.models.stock_universe import StockUniverse
from app.services.cjk_alias_resolver_service import (
    METHOD_SYMBOL_NORMALIZED, METHOD_SYMBOL_PASSTHROUGH, resolve_alias,
)
from app.services.multi_market_ticker_validator import TICKER_SHAPE_RE
from app.services.security_master_service import security_master_resolver


# Depositary receipts are second listings; funds are flagged as common stock in
# the universe but are not companies.
# ponytail: name heuristics also exclude some REITs ("Realty Trust"); those
# keep needing attestation, as before.
_NOT_SINGLE_COMPANY = re.compile(
    r"\b(adrs?|ads|depositary|etfs?|etns?|etps?|fund|index|trust|portfolio|shares)\b|\b\d+x\b",
    re.IGNORECASE,
)
# ponytail: A+H dual listings share no name text, so HK and mainland listings
# derive no identity while the other market is loaded; ISIN links would lift this.
_SAME_ISSUER_MARKETS = {"HK": "CN", "CN": "HK"}


class SocialTickerResolver:
    def __init__(self, db, *, verified_company_ids: Mapping[str, str] | None = None,
                 security_kinds: Mapping[str, str] | None = None):
        self.db = db
        # Caller supplies independently verified identities, never model/name guesses.
        self.company_ids = MappingProxyType(dict(verified_company_ids or {}))
        if any(not isinstance(value, str) or not value.strip() for value in self.company_ids.values()):
            raise ValueError("invalid_verified_company_id")
        self.security_kinds = MappingProxyType({"SPY": "broad_etf", "QQQ": "broad_etf",
            "SMH": "thematic_etf", "REMX": "thematic_etf", **(security_kinds or {})})
        self._listing_census = None  # (loaded markets, repeated names), read once

    def resolve(self, raw_token: str, explicit_market: str | None = None) -> TickerResolution:
        def unresolved(reason):
            return TickerResolution(raw_token, None, None, None, "unresolved", (reason,))

        if explicit_market is not None and explicit_market not in SUPPORTED_MARKETS:
            return unresolved("unsupported_market")
        token = security_master_resolver.normalize_symbol(raw_token)
        raw = raw_token.strip().lstrip("$")
        explicit = raw_token.strip().startswith("$") or (raw == raw.upper() and bool(TICKER_SHAPE_RE.fullmatch(raw)))
        with self.db.no_autoflush:
            # An explicit ticker is never redirected to a company alias (HSBC).
            if explicit:
                canonical = token
                if token.isdigit():
                    if explicit_market is None:
                        return unresolved("ambiguous_numeric_listing")
                    numeric = token.zfill(4) if explicit_market == "HK" else token
                    alias = resolve_alias(numeric, hint_market=explicit_market)
                    canonical = alias.canonical_symbol
                elif "." in token:
                    alias = resolve_alias(token, hint_market=explicit_market)
                    if alias.method in {METHOD_SYMBOL_NORMALIZED, METHOD_SYMBOL_PASSTHROUGH}:
                        canonical = alias.canonical_symbol
            else:
                alias = resolve_alias(raw, hint_market=explicit_market)
                canonical = alias.canonical_symbol
            if not canonical or not TICKER_SHAPE_RE.fullmatch(canonical):
                return unresolved("unknown_listing")
            security = self.db.query(StockUniverse).filter(
                StockUniverse.symbol == canonical, StockUniverse.active_filter()).first()
            if security is None:
                return unresolved("not_in_active_universe")
            if security.market not in SUPPORTED_MARKETS:
                return unresolved("unsupported_market")
            if explicit_market is not None and security.market != explicit_market:
                return unresolved("conflicting_market")
            kind = self.security_kinds.get(canonical, "stock" if security.is_common_stock else "macro")
            company_id = self.company_ids.get(canonical)
            derived = company_id is None and kind == "stock" and self._single_listing(security)
            related = ()
            if company_id:
                candidates = [symbol for symbol, identity in self.company_ids.items()
                              if identity == company_id and symbol != canonical]
                if candidates:
                    related = tuple(row.symbol for row in self.db.query(StockUniverse).filter(
                        StockUniverse.symbol.in_(candidates), StockUniverse.active_filter(),
                        StockUniverse.market.in_(SUPPORTED_MARKETS)).order_by(StockUniverse.symbol).all())
            if derived:
                company_id = f"security:{security.id}"
            reasons = (("company_identity_single_listing",) if derived
                       else () if company_id else ("company_identity_unknown",))
            return TickerResolution(raw_token, canonical, security.market, str(security.id), "resolved",
                reasons, company_id, related, kind, kind in {"stock", "thematic_etf"},
                bool(company_id) and kind == "stock", explicit)

    def company_id_for(self, security) -> str | None:
        """The attested or single-listing identity of a known universe row."""
        attested = self.company_ids.get(security.symbol)
        if attested:
            return attested
        stock = security.is_common_stock and security.symbol not in self.security_kinds
        return f"security:{security.id}" if stock and self._single_listing(security) else None

    def _single_listing(self, security) -> bool:
        name = (security.name or "").strip().lower()
        if not name or _NOT_SINGLE_COMPANY.search(name):
            return False
        if self._listing_census is None:
            normalized = func.lower(func.trim(StockUniverse.name))
            rows = self.db.query(StockUniverse.market, normalized, func.count()).filter(
                StockUniverse.active_filter()).group_by(StockUniverse.market, normalized).all()
            self._listing_census = ({market for market, _, _ in rows},
                                    {(market, value) for market, value, count in rows if value and count > 1})
        markets, repeated = self._listing_census
        if _SAME_ISSUER_MARKETS.get(security.market) in markets:
            return False
        return (security.market, name) not in repeated
