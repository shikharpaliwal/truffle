import logging

from ..http import ApiError

log = logging.getLogger(__name__)
CG = "https://api.coingecko.com/api/v3"
GREEN_MIN = 7  # CoinGecko paints trust_score 7-10 green


def trust_table(http, pages=2):
    """exchange_id -> numeric trust_score. Fetched once per run and disk-cached."""
    out = {}
    for page in range(1, pages + 1):
        try:
            rows = http.get_json("coingecko", f"{CG}/exchanges", {"per_page": 250, "page": page})
        except ApiError as e:
            log.warning("exchanges page %d failed: %s", page, e)
            break
        if not rows:
            break
        for r in rows:
            out[r["id"]] = r.get("trust_score")
    log.info("trust table: %d exchanges, %d green", len(out), sum(1 for v in out.values() if v and v >= GREEN_MIN))
    return out


def fetch_tickers(http, coin_id, pages=1):
    tickers = []
    for page in range(1, pages + 1):
        body = http.get_json("coingecko", f"{CG}/coins/{coin_id}/tickers",
                             {"page": page, "depth": "false"}, allow_404=True)
        rows = (body or {}).get("tickers") or []
        tickers.extend(rows)
        if len(rows) < 100:
            break
    return tickers


def reputable_share(tickers, trust):
    """Fraction of 24h ticker volume sitting on green-trust-score exchanges."""
    total = green = 0.0
    for t in tickers:
        vol = ((t.get("converted_volume") or {}).get("usd")) or 0.0
        if vol <= 0:
            continue
        total += vol
        ts = trust.get((t.get("market") or {}).get("identifier"))
        if (ts is not None and ts >= GREEN_MIN) or t.get("trust_score") == "green":
            green += vol
    return (green / total) if total else None


def exchange_count(tickers, trust, green_only=True):
    ids = set()
    for t in tickers:
        ident = (t.get("market") or {}).get("identifier")
        if not ident:
            continue
        if green_only:
            ts = trust.get(ident)
            if not (ts is not None and ts >= GREEN_MIN) and t.get("trust_score") != "green":
                continue
        ids.add(ident)
    return len(ids) or None
