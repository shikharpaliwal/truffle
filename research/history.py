"""Historical daily price/volume loader for backtests. Cached forever; read-only on the db."""
import logging
from datetime import datetime, timezone

from truffle.db import add_usage, usage_this_month

log = logging.getLogger(__name__)
CG = "https://api.coingecko.com/api/v3"
CG_HOST = "api.coingecko.com"
DAY_MS = 86_400_000


def check_budget(con, cfg, projected, force=False):
    budget = (cfg["http"].get("monthly_budget") or {}).get(CG_HOST)
    used = usage_this_month(con, CG_HOST)
    if not budget:
        return
    log.info("coingecko budget: %d/%d credits used this month, %d remain; this job projects <=%d",
             used, budget, budget - used, projected)
    if projected > budget - used and not force:
        raise SystemExit(f"ABORT: ~{projected} credits needed, only {budget - used} remain this month.")


def record_usage(con, http):
    for host, n in http.per_host.items():
        add_usage(con, host, n)
    con.commit()


def fetch_chart(http, coin_id, days=365):
    return http.get_json("coingecko", f"{CG}/coins/{coin_id}/market_chart",
                         {"vs_currency": "usd", "days": days}, allow_404=True)


def to_daily(body):
    """{'YYYY-MM-DD': (price, volume, mcap)} from a market_chart body.

    Keeps only samples landing exactly on a UTC midnight, which drops CoinGecko's
    trailing partial-day point -- that point is "now" and would be lookahead.
    """
    if not body or not body.get("prices"):
        return {}
    cols = {}
    for name, key in (("p", "prices"), ("v", "total_volumes"), ("m", "market_caps")):
        cols[name] = {ts: val for ts, val in (body.get(key) or []) if ts % DAY_MS == 0}
    out = {}
    for ts, price in cols["p"].items():
        if price is None:
            continue
        d = datetime.fromtimestamp(ts / 1000, timezone.utc).strftime("%Y-%m-%d")
        out[d] = (price, cols["v"].get(ts), cols["m"].get(ts))
    return out


def load(http, coin_ids, days=365):
    """{coin_id: {date: (price, vol, mcap)}}. Missing/404 coins are skipped."""
    out, failed = {}, []
    for i, cid in enumerate(coin_ids, 1):
        try:
            series = to_daily(fetch_chart(http, cid, days))
        except Exception as e:  # noqa: BLE001 - one bad coin must not kill a 10-minute job
            log.warning("%s: %s", cid, e)
            failed.append(cid)
            continue
        if len(series) < 120:
            failed.append(cid)
            continue
        out[cid] = series
        if i % 50 == 0:
            log.info("  loaded %d/%d", i, len(coin_ids))
    if failed:
        log.info("dropped %d coins with no/short history", len(failed))
    return out
