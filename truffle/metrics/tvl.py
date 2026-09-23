import logging

log = logging.getLogger(__name__)
LLAMA = "https://api.llama.fi"
DAY = 86_400


def slug_map(http):
    """gecko_id -> defillama slug, from the single /protocols listing."""
    rows = http.get_json("defillama", f"{LLAMA}/protocols")
    best = {}
    # Several protocols can share a gecko_id (aave-v2 / aave-v3, or a chain entity
    # alongside the real protocol). Keep the one with the most TVL, not the first seen.
    for r in rows or []:
        gid, slug = r.get("gecko_id"), r.get("slug")
        if not gid or not slug:
            continue
        tvl = r.get("tvl") or -1
        if gid not in best or tvl > best[gid][1]:
            best[gid] = (slug, tvl)
    out = {gid: slug for gid, (slug, _) in best.items()}
    log.info("defillama: %d protocols, %d with a gecko_id", len(rows or []), len(out))
    return out


def fetch_tvl(http, slug, window_days):
    body = http.get_json("defillama", f"{LLAMA}/protocol/{slug}", allow_404=True)
    series = (body or {}).get("tvl")
    if not isinstance(series, list) or not series:
        return None, None
    pts = [(p["date"], p.get("totalLiquidityUSD")) for p in series if p.get("totalLiquidityUSD") is not None]
    if not pts:
        return None, None
    end = pts[-1][0]
    # TVL is a stock, not a flow: compare the window's mean level, not a sum.
    cur = [v for t, v in pts if t > end - window_days * DAY]
    prior = [v for t, v in pts if end - 2 * window_days * DAY < t <= end - window_days * DAY]
    return (sum(cur) / len(cur) if cur else None,
            sum(prior) / len(prior) if prior else None)
