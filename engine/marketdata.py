"""Finnhub quote fetching and symbol resolution.

Free tier: 60 calls/min — we pace to ~55/min. Verified reachable on this plan
(2026-07-27): every US-listed instrument (equities, ADRs, ETFs incl. inverse,
leveraged and volatility ETPs) and any crypto pair via an exchange-prefixed
source symbol. NOT reachable: forex, international listings, indices, option
chains — all 403/unsupported, which is why `resolve` verifies a live quote
before any symbol is allowed onto the watchlist.
"""
import os
import re
import time
from datetime import datetime, timezone

import requests

BASE = "https://finnhub.io/api/v1"

# Crypto has no listing record to look up — we probe these venues in order and
# take the first that quotes. Order matters: Binance USDT pairs cover the most
# assets; Coinbase is the fallback for USD-native pairs.
CRYPTO_VENUES = ("BINANCE:{base}USDT", "BINANCE:{base}USD", "COINBASE:{base}-USD")
CRYPTO_RE = re.compile(r"^([A-Z0-9]{2,10})-?USDT?$")
# Crypto venues list fiat pairs too (BINANCE:EURUSDT quotes fine), which would
# smuggle forex — explicitly out of scope, and not crypto — in through the
# crypto branch under a crypto label. The base currency decides, not the venue.
FIAT_CODES = frozenset(
    "EUR GBP JPY CHF AUD CAD NZD CNY CNH HKD SGD KRW INR TRY BRL MXN ZAR RUB "
    "SEK NOK DKK PLN HUF CZK ILS THB IDR PHP MYR AED SAR ARS CLP COP".split()
)

# Fund-name markers for the hazard class: leveraged, inverse, and volatility
# ETPs. Deliberately biased toward over-classification — a plain ETF wrongly
# put in this class produces a visible, correctable rejection, while a 3x fund
# wrongly classed as a plain ETF silently defeats an agent's class cap.
_LEVERED_MARKERS = re.compile(
    r"\d\s*X\b"            # 3X, 2X, B3X, B2X
    r"|ULTRA"              # ULTRAPRO, ULTRASHORT, ULTRA VIX
    r"|\bVIX\b"            # vol-futures ETPs decay like levered products
    r"|\bINVERSE\b"
    r"|\bDIREXION\b|\bDIREX\b|\bDIRXN\b|\bDIRX\b|\bDIR\b"  # Direxion: levered house
)
# "SHORT S&P500" is inverse; "SHORT-TERM TREASURY" is a duration, not a bet.
_SHORT_MARKER = re.compile(r"\bSHORT\b(?![- ]TERM)")


class QuoteError(Exception):
    pass


def _get(path, params, attempts=3):
    """A GET, retried with exponential backoff on transient network/HTTP errors
    (timeouts, connection resets, 429/5xx). Returns the JSON payload, or None if
    every attempt failed — so a brief Finnhub blip self-heals within the tick
    instead of skipping every symbol and returning an empty batch that aborts
    the whole run.

    None means "we could not ask", which is never the same fact as "the answer
    is no" — callers must not collapse the two."""
    delay = 0.5
    for attempt in range(attempts):
        try:
            r = requests.get(f"{BASE}{path}", params=params, timeout=15)
            r.raise_for_status()
            return r.json()
        except (requests.RequestException, ValueError):
            if attempt == attempts - 1:
                return None
            time.sleep(delay)
            delay *= 2
    return None


def _quote(src, key, attempts=3):
    """One symbol's raw quote payload, or None if the data source was
    unreachable. A payload with c == 0 is an answer: no data for this symbol."""
    return _get("/quote", {"symbol": src, "token": key}, attempts)


def _normalize(q):
    """A raw /quote payload → the engine's tick shape, or None if it has no
    usable price (0 or absent = unknown symbol / no data on this plan).

    high/low/open are the session extremes the payload carries alongside the
    last price. Verified 2026-07-29, live during pre-market: the free tier
    freezes c/h/l/o/t at the previous close until the next regular session
    opens, so h/l describe the regular session only — no extended-hours
    prints — and t names the session they belong to. The fill logic leans on
    both facts; re-verify if the plan changes."""
    if not q or not q.get("c"):
        return None
    ts = q.get("t") or 0
    return {
        "price": float(q["c"]),
        "prev_close": float(q["pc"]) if q.get("pc") else None,
        "high": float(q["h"]) if q.get("h") else None,
        "low": float(q["l"]) if q.get("l") else None,
        "open": float(q["o"]) if q.get("o") else None,
        "ts": datetime.fromtimestamp(ts, tz=timezone.utc)
        if ts
        else datetime.now(timezone.utc),
    }


def classify_listing(listing):
    """A /search result → asset class. `type` separates stock from fund; the
    fund's name is the only free-tier signal for leverage and inversion."""
    desc = (listing.get("description") or "").upper()
    if listing.get("type") != "ETP":
        return "equity"
    if _LEVERED_MARKERS.search(desc) or _SHORT_MARKER.search(desc):
        return "inverse_levered"
    return "etf"


def _exact_listing(symbol, key):
    """The listing whose ticker is exactly `symbol`, or None if there is no
    such listing. /search returns every venue's copy (TQQQ, TQQQ.TO, TQQQ.BA);
    only the bare US ticker is quotable on this plan, so an exact match is the
    only acceptable one. Raises QuoteError if the search could not be made —
    an unreachable data source must never be reported as "no such symbol"."""
    payload = _get("/search", {"q": symbol, "token": key})
    if payload is None:
        raise QuoteError(f"search for {symbol} failed: data source unreachable")
    results = payload.get("result") or []
    return next((x for x in results if (x.get("symbol") or "") == symbol), None)


def resolve(symbol):
    """Resolve an internal symbol against the data source, verifying it really
    quotes before it is allowed to exist in the arena.

    Returns {source_symbol, asset_class, description, quote} or None. None
    means "we cannot price this" — the only honest answer for forex, foreign
    listings, indices and typos, all of which used to be granted onto the
    watchlist and then silently never quote.

    Raises QuoteError when the data source could not be reached. That is not a
    verdict on the symbol and callers must not present it as one: a rate-limited
    lookup telling an agent its instrument does not exist is the engine
    inventing a fact.
    """
    key = os.environ["FINNHUB_KEY"]
    sym = (symbol or "").strip().upper()
    if not sym:
        return None

    candidates = []
    listing = _exact_listing(sym, key)
    if listing:
        candidates.append(
            (sym, classify_listing(listing), listing.get("description") or sym)
        )
    m = CRYPTO_RE.match(sym)
    if m and m.group(1) not in FIAT_CODES:
        candidates += [
            (v.format(base=m.group(1)), "crypto", f"{m.group(1)} spot")
            for v in CRYPTO_VENUES
        ]

    for source_symbol, asset_class, description in candidates:
        raw = _quote(source_symbol, key)
        if raw is None:
            raise QuoteError(f"quote for {source_symbol} failed: data source unreachable")
        quote = _normalize(raw)
        if quote:
            return {
                "source_symbol": source_symbol,
                "asset_class": asset_class,
                "description": description,
                "quote": quote,
            }
    return None


def fetch_quotes(symbol_map):
    """symbol_map: {internal_symbol: source_symbol}. Returns
    {internal_symbol: {price, prev_close, ts}} for every symbol that returned
    a sane quote; symbols that fail are simply absent (caller decides policy)."""
    key = os.environ["FINNHUB_KEY"]
    out = {}
    for i, (sym, src) in enumerate(sorted(symbol_map.items())):
        if i and i % 50 == 0:
            time.sleep(60)  # stay under the per-minute cap on big universes
        tick = _normalize(_quote(src, key))
        if tick:
            # Exchange-prefixed symbols (crypto) report a rolling 24h window,
            # not a session — their h/l can predate anything and must never
            # drive the touched-since-last-look fill logic.
            tick["session_range"] = ":" not in src
            out[sym] = tick
        time.sleep(0.15)
    return out
