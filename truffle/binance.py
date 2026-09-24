import logging

log = logging.getLogger(__name__)

SPOT = "https://api.binance.com/api/v3"
FAPI = "https://fapi.binance.com/fapi/v1"
HOUR_MS = 3_600_000
MAX_LIMIT = 1000        # klines caps here; limit>1000 is silently truncated
# Spot klines cost weight 2 at any limit, so page as wide as possible. Futures klines
# cost 2 below limit 500 and 5 at or above it, so 90 days is cheaper in five 499-bar
# pages (10 weight) than in three 1000-bar pages (15) against a tighter 2400/min cap.
PAGE = {"spot": MAX_LIMIT, "perp": 499}

# Binance responses are large and single-use, so none of them go through the disk cache.
_NOCACHE = {"cache": False}


def _base(market):
    return FAPI if market == "perp" else SPOT


def usdt_symbols(http, market):
    """TRADING USDT pairs. Futures are restricted to perpetuals: dated contracts expire
    and would leave dead symbols in the tracking set."""
    d = http.get_json("binance", f"{_base(market)}/exchangeInfo", **_NOCACHE)
    out = [s for s in d["symbols"] if s["status"] == "TRADING" and s["quoteAsset"] == "USDT"]
    if market == "perp":
        out = [s for s in out if s.get("contractType") == "PERPETUAL"]
    return out


def usdt_prices(http, market):
    """symbol -> USD price, in one call. Spot uses the 24h ticker (weight 80), perps the
    premium index (weight 10), which is the only free endpoint covering every contract."""
    if market == "perp":
        rows = http.get_json("binance", f"{FAPI}/premiumIndex", **_NOCACHE)
        return {r["symbol"]: float(r["markPrice"]) for r in rows if float(r["markPrice"]) > 0}
    rows = http.get_json("binance", f"{SPOT}/ticker/24hr", **_NOCACHE)
    return {r["symbol"]: float(r["lastPrice"]) for r in rows if float(r["lastPrice"]) > 0}


def klines(http, symbol, market, start_ms=None, limit=None):
    p = {"symbol": symbol, "interval": "1h", "limit": min(limit or PAGE[market], MAX_LIMIT)}
    if start_ms is not None:
        p["startTime"] = start_ms
    return http.get_json("binance", f"{_base(market)}/klines", p, **_NOCACHE) or []


def parse_kline(row):
    """-> (open_time, o, h, l, c, base_volume, quote_volume, trades, close_time)."""
    return (int(row[0]), float(row[1]), float(row[2]), float(row[3]), float(row[4]),
            float(row[5]), float(row[7]), int(row[8]), int(row[6]))


def funding(http, symbol, start_ms=None, limit=MAX_LIMIT):
    p = {"symbol": symbol, "limit": min(limit, MAX_LIMIT)}
    if start_ms is not None:
        p["startTime"] = start_ms
    rows = http.get_json("binance", f"{FAPI}/fundingRate", p, **_NOCACHE) or []
    return [(symbol, int(r["fundingTime"]), float(r["fundingRate"]), float(r.get("markPrice") or 0) or None)
            for r in rows]
