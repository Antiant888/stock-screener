import pytest

from app.models.stock_universe import StockUniverse


@pytest.fixture
def resolver(db_session):
    from app.services.social_ticker_resolver import SocialTickerResolver
    for symbol, market in [("HSBC", "US"), ("0005.HK", "HK"), ("2330.TW", "TW"),
                           ("TSM", "US"), ("6758.T", "JP"), ("600519.SS", "CN"),
                           ("SPY", "US"), ("QQQ", "US"), ("SMH", "US"), ("REMX", "US")]:
        db_session.add(StockUniverse(symbol=symbol, market=market, is_active=True))
    db_session.add(StockUniverse(symbol="DEAD", market="US", is_active=False))
    db_session.flush()
    return SocialTickerResolver(db_session, verified_company_ids={
        "HSBC": "company:hsbc", "0005.HK": "company:hsbc",
        "TSM": "company:tsmc", "2330.TW": "company:tsmc"})


@pytest.mark.parametrize("token,market,want", [
    ("$HSBC", None, "HSBC"), ("HSBC", None, "HSBC"),
    ("$0005.HK", None, "0005.HK"), ("$600519.SS", None, "600519.SS"),
    ("$5.HK", None, "0005.HK"), ("$5", "HK", "0005.HK"),
    ("$6758.T", None, "6758.T"), ("$2330.TW", None, "2330.TW"),
    ("$2330", "TW", "2330.TW"), ("台積電", None, "2330.TW"),
    ("Sony", None, "6758.T"), ("ソニー", None, "6758.T"),
    ("$TSM", None, "TSM"),
])
def test_explicit_listing_precedes_company_alias(resolver, token, market, want):
    result = resolver.resolve(token, market)
    assert (result.status, result.symbol) == ("resolved", want)


@pytest.mark.parametrize("token,market", [("unknown business", None), ("DEAD", None),
    ("$HSBC", "HK"), ("6758", None), ("$HSBC", "ZZ"), ("$2330.TW", "US")])
def test_unknown_inactive_or_conflicting_listing_remains_unresolved(resolver, token, market):
    result = resolver.resolve(token, market)
    assert result.status == "unresolved"
    assert result.market is None and result.security_id is None


def test_related_instruments_do_not_replace_two_explicit_listings(resolver):
    us, hk = resolver.resolve("$HSBC", None), resolver.resolve("$0005.HK", None)
    assert us.symbol != hk.symbol
    assert us.company_id == hk.company_id == "company:hsbc"
    assert us.related_symbols == ("0005.HK",)
    assert hk.related_symbols == ("HSBC",)
    assert us.company_count_eligible and hk.company_count_eligible


def test_no_company_linkage_is_fabricated(resolver):
    result = resolver.resolve("6758.T", None)
    assert result.company_id is None and not result.company_count_eligible
    assert result.ranking_eligible and result.related_symbols == ()
    assert "company_identity_unknown" in result.reason_codes


@pytest.mark.parametrize("symbol,kind,eligible", [("SPY", "broad_etf", False),
    ("QQQ", "broad_etf", False), ("SMH", "thematic_etf", True), ("REMX", "thematic_etf", True)])
def test_etf_context_and_thematic_eligibility(resolver, symbol, kind, eligible):
    result = resolver.resolve(symbol, None)
    assert result.security_kind == kind
    assert result.ranking_eligible is eligible
    assert not result.company_count_eligible


def _named(db, symbol, market, name):
    row = StockUniverse(symbol=symbol, market=market, name=name, is_active=True)
    db.add(row)
    db.flush()
    return row


def test_single_listing_common_stock_derives_its_own_company_identity(db_session):
    from app.services.social_ticker_resolver import SocialTickerResolver
    row = _named(db_session, "CEG", "US", "Constellation Energy Corp")
    result = SocialTickerResolver(db_session).resolve("$CEG")
    assert result.company_id == f"security:{row.id}"
    assert result.company_count_eligible
    assert result.reason_codes == ("company_identity_single_listing",)


@pytest.mark.parametrize("rows,token", [
    ([("GOOGL", "US", "Alphabet Inc"), ("GOOG", "US", "Alphabet Inc")], "$GOOGL"),
    ([("BABA", "US", "Alibaba Group Holding Ltd ADR")], "$BABA"),
    ([("XYZ", "US", "Some Holdings ADS")], "$XYZ"),
    ([("0700.HK", "HK", "TENCENT"), ("600519.SS", "CN", "Kweichow Moutai")], "$0700.HK"),
    ([("NONAME", "US", None)], "$NONAME"),
    # The universe flags many funds as common stock; a fund is not a company.
    ([("IWM", "US", "iShares Russell 2000 Index Fund")], "$IWM"),
    ([("XLF", "US", "State Street Financial Select Sector SPDR ETF")], "$XLF"),
    ([("TQQQ", "US", "ProShares UltraPro QQQ 3x Shares")], "$TQQQ"),
    ([("GSG", "US", "iShares S&P GSCI Commodity Indexed Trust")], "$GSG"),
])
def test_possible_cross_listings_and_funds_still_need_attestation(db_session, rows, token):
    from app.services.social_ticker_resolver import SocialTickerResolver
    for symbol, market, name in rows:
        _named(db_session, symbol, market, name)
    result = SocialTickerResolver(db_session).resolve(token)
    assert result.status == "resolved"
    assert result.company_id is None and not result.company_count_eligible
    assert result.reason_codes == ("company_identity_unknown",)


def test_hk_listing_derives_identity_while_no_mainland_market_is_loaded(db_session):
    from app.services.social_ticker_resolver import SocialTickerResolver
    _named(db_session, "0700.HK", "HK", "TENCENT")
    assert SocialTickerResolver(db_session).resolve("$0700.HK").company_count_eligible


def test_attested_identity_wins_over_a_derived_one(db_session):
    from app.services.social_ticker_resolver import SocialTickerResolver
    row = _named(db_session, "CEG", "US", "Constellation Energy Corp")
    resolver = SocialTickerResolver(db_session, verified_company_ids={"CEG": "company:ceg"})
    assert resolver.resolve("$CEG").company_id == "company:ceg"
    assert resolver.company_id_for(row) == "company:ceg"
    assert SocialTickerResolver(db_session).company_id_for(row) == f"security:{row.id}"
