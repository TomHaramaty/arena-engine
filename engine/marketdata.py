"""Finnhub quote fetching. Free tier: 60 calls/min — we pace to ~55/min."""
import os
import time
from datetime import datetime, timezone

import requests

BASE = "https://finnhub.io/api/v1"


class QuoteError(Exception):
    pass


def fetch_quotes(symbol_map):
    """symbol_map: {internal_symbol: source_symbol}. Returns
    {internal_symbol: {price, prev_close, ts}} for every symbol that returned
    a sane quote; symbols that fail are simply absent (caller decides policy)."""
    key = os.environ["FINNHUB_KEY"]
    out = {}
    for i, (sym, src) in enumerate(sorted(symbol_map.items())):
        if i and i % 50 == 0:
            time.sleep(60)  # stay under the per-minute cap on big universes
        try:
            r = requests.get(
                f"{BASE}/quote", params={"symbol": src, "token": key}, timeout=15
            )
            r.raise_for_status()
            q = r.json()
        except requests.RequestException:
            continue
        price = q.get("c")
        if not price:  # 0 or None → unknown symbol / no data
            continue
        ts = q.get("t") or 0
        out[sym] = {
            "price": float(price),
            "prev_close": float(q["pc"]) if q.get("pc") else None,
            "ts": datetime.fromtimestamp(ts, tz=timezone.utc)
            if ts
            else datetime.now(timezone.utc),
        }
        time.sleep(0.15)
    return out
