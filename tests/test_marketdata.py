"""Symbol resolution and asset classification.

The description strings below are verbatim /search payloads captured from the
live free-tier API on 2026-07-27 — abbreviations, truncation and all. A
classifier that only handles tidy fund names is a classifier that will put a 3x
fund in the plain-ETF bucket in production.
"""
import pytest

from engine import marketdata as md


def _listing(symbol, type_, description):
    return {"symbol": symbol, "type": type_, "description": description}


LEVERED = [
    ("SH", "PROSHARES SHORT S&P500"),
    ("PSQ", "PROSHARES SHORT QQQ"),
    ("SQQQ", "PROSHARES ULTRAPRO SHORT QQQ"),
    ("TQQQ", "PROSHARES ULTRAPRO QQQ"),
    ("SOXL", "DIREX DAIL SEMI BU 3X ET-USD"),
    ("SOXS", "DIREX DAI SEMI BE 3X ETF-USD"),
    ("UPRO", "PROSHARES ULTRAPRO S&P 500"),
    ("SPXU", "PROSH ULTRAPRO SHORT S&P 500"),
    ("UVXY", "PROSHARES ULTRA VIX ST FUTUR"),
    ("VIXY", "PROSHARES VIX SHORT-TERM FUT"),
    ("SDS", "PROSHARES ULTRASHORT S&P500"),
    ("QID", "PROSHARES ULTRASHORT QQQ"),
    ("DOG", "PROSHARES SHORT DOW30"),
    ("RWM", "PROSHARES SHORT RUSSELL2000"),
    ("TZA", "DIRX DLY SMAL CAP B3X ETF-UI"),
    ("TNA", "DIR DAI SML CA BUL 3X ET-USD"),
    ("SVXY", "PROSHARES SHORT VIX ST FUTUR"),
    ("YANG", "DIR DLY FT CHI BE 3X ETF-USD"),
    ("YINN", "DIREXION DLY FTSE CH BL3X-UI"),
    ("NUGT", "DIRXN DLY GM INDX B2X ETF-UI"),
    ("DUST", "DIR DLY GOL MN IDX BR 2X ETF"),
]

# The dangerous direction: funds whose names carry "SHORT" (a duration), "INV"
# (investment grade) or digits, and which must NOT be caged in the hazard class.
PLAIN_ETFS = [
    ("SHV", "ISHARES 0-1 YEAR TREASURY BO"),
    ("SHY", "ISHARES 1-3 YEAR TREASURY BO"),
    ("VGSH", "VANGUARD SHORT-TERM TREASURY"),
    ("SCHO", "SCHWAB SHORT-TERM US TREAS"),
    ("IGSB", "ISHARES 1-5Y INV GRADE CORP"),
    ("BSV", "VANGUARD SHORT-TERM BOND ETF"),
    ("FLOT", "ISHARES FLOATING RATE BOND E"),
    ("SJNK", "SS SPDR BB ST HI YIELD ETF"),
    ("XHB", "SS SPDR S&P HOMEBLDRS ETF"),
    ("SPY", "SS SPDR S&P 500 ETF TRUST-US"),
    ("QQQ", "INVESCO QQQ TRUST SERIES 1"),
    ("GLD", "SPDR GOLD SHARES"),
    ("TLT", "ISHARES 20+ YEAR TREASURY BD"),
    ("IBIT", "ISHARES BITCOIN TRUST ETF"),
    ("ARKK", "ARK INNOVATION ETF"),
    ("EWJ", "ISHARES MSCI JAPAN ETF"),
    ("FXI", "ISHARES CHINA LARGE-CAP ETF"),
    ("HYG", "ISHR IBX USD HIYLD CB ETF-UI"),
    ("DBC", "INVESCO DB COMMODITY INDEX T"),
    ("UNG", "US NATURAL GAS FUND LP"),
]


def test_classify_levered_and_inverse():
    for sym, desc in LEVERED:
        assert md.classify_listing(_listing(sym, "ETP", desc)) == "inverse_levered", sym


def test_classify_plain_etfs():
    for sym, desc in PLAIN_ETFS:
        assert md.classify_listing(_listing(sym, "ETP", desc)) == "etf", sym


def test_classify_common_stock():
    assert md.classify_listing(
        _listing("RXRX", "Common Stock", "RECURSION PHARMACEUTICALS-A")
    ) == "equity"
    # A stock is a stock even if its name would trip the fund heuristics.
    assert md.classify_listing(
        _listing("BULL", "Common Stock", "WEBULL CORP 3X SHORT")
    ) == "equity"


def _stub(monkeypatch, listings, prices):
    """Wire resolve() to fixed /search and /quote answers."""
    monkeypatch.setenv("FINNHUB_KEY", "test")
    monkeypatch.setattr(
        md, "_exact_listing", lambda sym, key: listings.get(sym)
    )
    monkeypatch.setattr(
        md, "_quote", lambda src, key, attempts=3:
        {"c": prices[src], "pc": prices[src], "t": 0} if src in prices else {"c": 0}
    )


def test_resolve_listed_equity(monkeypatch):
    _stub(monkeypatch,
          {"RXRX": _listing("RXRX", "Common Stock", "RECURSION PHARMACEUTICALS-A")},
          {"RXRX": 2.96})
    r = md.resolve("rxrx")
    assert r["source_symbol"] == "RXRX"
    assert r["asset_class"] == "equity"
    assert r["quote"]["price"] == 2.96


def test_resolve_crypto_takes_first_venue_that_quotes(monkeypatch):
    # No listing record exists for crypto; Binance USDT is probed first.
    _stub(monkeypatch, {}, {"BINANCE:SOLUSDT": 76.66, "COINBASE:SOL-USD": 76.70})
    r = md.resolve("SOL-USD")
    assert r["source_symbol"] == "BINANCE:SOLUSDT"
    assert r["asset_class"] == "crypto"


def test_resolve_crypto_falls_through_to_coinbase(monkeypatch):
    _stub(monkeypatch, {}, {"COINBASE:XRP-USD": 2.11})
    r = md.resolve("XRP-USD")
    assert r["source_symbol"] == "COINBASE:XRP-USD"


def test_resolve_rejects_unquotable(monkeypatch):
    # Forex, foreign listings and typos: no listing record, no crypto venue.
    _stub(monkeypatch, {}, {})
    assert md.resolve("EURUSD") is None
    assert md.resolve("SAP.DE") is None
    assert md.resolve("NOTATICKER") is None


def test_resolve_rejects_fiat_pairs(monkeypatch):
    """Binance quotes EURUSDT, so the crypto branch would happily hand back
    forex — out of scope — wearing a crypto label. The base currency decides."""
    _stub(monkeypatch, {}, {"BINANCE:EURUSDT": 1.14, "BINANCE:JPYUSDT": 0.0064})
    assert md.resolve("EURUSD") is None
    assert md.resolve("EUR-USD") is None
    assert md.resolve("JPYUSD") is None


def test_resolve_rejects_listed_but_unpriced(monkeypatch):
    """A listing we cannot price on this plan is a rejection, not a grant —
    the old code granted these and then failed to quote them forever."""
    _stub(monkeypatch, {"SAP.DE": _listing("SAP.DE", "Common Stock", "SAP SE")}, {})
    assert md.resolve("SAP.DE") is None


def test_unreachable_search_raises_rather_than_denying(monkeypatch):
    """A rate-limited or down data source must not be reported as "no such
    symbol" — that is the engine inventing a fact about the market."""
    monkeypatch.setenv("FINNHUB_KEY", "test")
    monkeypatch.setattr(md, "_get", lambda path, params, attempts=3: None)
    with pytest.raises(md.QuoteError):
        md.resolve("XLU")


def test_unreachable_quote_raises_rather_than_denying(monkeypatch):
    monkeypatch.setenv("FINNHUB_KEY", "test")
    monkeypatch.setattr(
        md, "_exact_listing",
        lambda sym, key: _listing("XLU", "ETP", "SS UTILITIES SELECT SECTOR"),
    )
    monkeypatch.setattr(md, "_quote", lambda src, key, attempts=3: None)
    with pytest.raises(md.QuoteError):
        md.resolve("XLU")


def test_zero_price_is_an_answer_not_an_outage(monkeypatch):
    """c == 0 means the source answered 'no data for this symbol' — a real
    rejection, distinct from an unreachable source."""
    _stub(monkeypatch, {}, {})
    assert md.resolve("NOTATICKER") is None


def test_resolve_prefers_the_listing_over_a_crypto_reading(monkeypatch):
    """COIN is a stock; nothing about its shape should send it to a venue."""
    _stub(monkeypatch,
          {"COIN": _listing("COIN", "Common Stock", "COINBASE GLOBAL INC-CLASS A")},
          {"COIN": 214.5})
    assert md.resolve("COIN")["asset_class"] == "equity"