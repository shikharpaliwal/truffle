import logging

from .http import ApiError

log = logging.getLogger(__name__)
CG = "https://api.coingecko.com/api/v3"
FIAT_TICKERS = ("USD", "EUR", "GBP", "JPY", "CHF", "TRY", "BRL")


def fetch_markets(http, limit):
    """Top-N by market cap. CoinGecko caps per_page at 250, so page as needed."""
    rows, page = [], 1
    while len(rows) < limit:
        want = min(250, limit - len(rows))
        batch = http.get_json("coingecko", f"{CG}/coins/markets", {
            "vs_currency": "usd", "order": "market_cap_desc",
            "per_page": want, "page": page, "sparkline": "false",
        })
        if not batch:
            break
        rows.extend(batch)
        if len(batch) < want:
            break
        page += 1
    return rows[:limit]


def fetch_excluded_ids(http, categories):
    """Coin ids belonging to an excluded CoinGecko category. One cached call per category."""
    out = {}
    for cat in categories:
        try:
            rows = http.get_json("coingecko", f"{CG}/coins/markets", {
                "vs_currency": "usd", "category": cat, "order": "market_cap_desc",
                "per_page": 250, "page": 1, "sparkline": "false",
            })
        except ApiError as e:
            log.warning("category %s unavailable (%s); falling back to heuristics", cat, e)
            continue
        for r in rows or []:
            out.setdefault(r["id"], f"category:{cat}")
    log.info("category exclusion list: %d coins across %d categories", len(out), len(categories))
    return out


def classify(markets, cfg, category_excluded):
    """Tag each market row with included / exclusion_reason. Returns rows in rank order."""
    ex = cfg["exclusions"]
    blocked = {s.upper() for s in ex["symbols"]}
    name_bits = [s.lower() for s in ex["name_contains"]]
    prefixes = sorted(ex["wrapper_prefixes"], key=len, reverse=True)
    tol = ex["peg_tolerance"]
    all_symbols = {(r.get("symbol") or "").upper() for r in markets}

    out = []
    for r in markets:
        sym, name = (r.get("symbol") or "").upper(), (r.get("name") or "").lower()
        price = r.get("current_price")
        reason = category_excluded.get(r["id"])
        if not reason and sym in blocked:
            reason = "symbol_blocklist"
        if not reason and any(b in name for b in name_bits):
            reason = "name_blocklist"
        if not reason:
            for p in prefixes:
                if sym.startswith(p) and len(sym) > len(p) and sym[len(p):] in all_symbols:
                    reason = f"wrapper_prefix:{p}"
                    break
        if not reason and price and any(f in sym for f in FIAT_TICKERS) and abs(price - 1.0) <= tol:
            reason = "peg_price"
        out.append({**r, "included": reason is None, "exclusion_reason": reason})
    return out


def build(http, cfg, limit):
    """Fetch an oversized slice, drop exclusions, keep the top `limit` survivors."""
    over = min(1000, int(limit * 1.6) + 20)
    markets = fetch_markets(http, over)
    cat_ex = fetch_excluded_ids(http, cfg["exclusions"]["categories"])
    rows = classify(markets, cfg, cat_ex)
    kept, snapshot = [], []
    for r in rows:
        if r["included"] and len(kept) < limit:
            kept.append(r)
            snapshot.append(r)
        else:
            snapshot.append({**r, "included": False,
                             "exclusion_reason": r["exclusion_reason"] or "beyond_universe_size"})
    log.info("universe: %d kept, %d excluded (of %d fetched)",
             len(kept), sum(1 for s in snapshot if not s["included"]), len(markets))
    return kept, snapshot
