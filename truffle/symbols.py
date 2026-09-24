import logging
from collections import defaultdict
from datetime import datetime, timezone

log = logging.getLogger(__name__)


def strip_multiplier(base, prefixes):
    """1000PEPE quotes 1000 PEPE. Longest prefix first, and never strip the whole symbol."""
    for p in sorted(prefixes, key=len, reverse=True):
        if base.startswith(p) and len(base) > len(p):
            return base[len(p):], int(p)
    return base, 1


def universe_coins(con):
    """The CoinGecko side of the mapping: the latest finished run's kept universe."""
    return [dict(r) for r in con.execute(
        "SELECT coin_id, symbol, name, market_cap, price_usd FROM universe_snapshots "
        "WHERE run_id=(SELECT MAX(id) FROM runs WHERE finished_at IS NOT NULL) AND included=1")]


def resolve(coins, pairs, prices, tolerance=2.0, prefixes=("1000000", "1000")):
    """Match Binance pairs to coin_ids on symbol, then confirm with price agreement.

    Symbol equality alone is unsafe (tickers collide across projects), so a match only
    stands when the Binance price and the price CoinGecko last reported for that coin
    agree within `tolerance`x. Anything else is flagged, never guessed at.
    Returns rows of (symbol, market, base_asset, multiplier, coin_id, status, note, price_ratio).
    """
    by_symbol = defaultdict(list)
    for c in coins:
        if c["symbol"]:
            by_symbol[c["symbol"].upper()].append(c)

    rows = []
    for p in pairs:
        sym, market = p["symbol"], p["market"]
        base, mult = strip_multiplier(p["baseAsset"].upper(), prefixes)
        cands = by_symbol.get(base, [])
        px = prices.get(sym)
        row = lambda cid, status, note, ratio=None: rows.append(  # noqa: E731
            (sym, market, p["baseAsset"].upper(), mult, cid, status, note, ratio))

        if not cands:
            row(None, "unmapped", "no coin with this symbol in the universe")
            continue
        if not px:
            row(None, "ambiguous", "no binance price to verify against")
            continue

        scored = [(c, (px / mult) / c["price_usd"]) for c in cands if c.get("price_usd")]
        agree = [(c, r) for c, r in scored if 1 / tolerance <= r <= tolerance]
        if len(agree) == 1:
            c, r = agree[0]
            note = f"disambiguated from {len(cands)} symbol matches by price" if len(cands) > 1 else None
            row(c["coin_id"], "mapped", note, round(r, 4))
        elif not agree:
            best = min(scored, key=lambda s: abs(1 - s[1]), default=None)
            ids = ",".join(c["coin_id"] for c in cands)
            row(None, "ambiguous", f"price disagrees with {ids}"
                + (f" (ratio {best[1]:.3g})" if best else " (no coingecko price)"),
                round(best[1], 4) if best else None)
        else:
            row(None, "ambiguous", "several universe coins agree: "
                + ",".join(c["coin_id"] for c, _ in agree))

    return _drop_duplicate_coins(rows)


def _drop_duplicate_coins(rows):
    """Two pairs in one market claiming the same coin means the symbol match is wrong
    somewhere (usually a multiplier strip). Flag both rather than pick one."""
    seen = defaultdict(list)
    for i, r in enumerate(rows):
        if r[5] == "mapped":
            seen[(r[1], r[4])].append(i)
    for (market, cid), idx in seen.items():
        if len(idx) > 1:
            others = ",".join(rows[i][0] for i in idx)
            for i in idx:
                rows[i] = (*rows[i][:4], None, "ambiguous", f"{cid} also claimed by {others}", rows[i][7])
    return rows


def store(con, rows):
    """Upsert the map. Rows flipped to manual=1 by hand are never overwritten."""
    manual = {(r["symbol"], r["market"]) for r in
              con.execute("SELECT symbol, market FROM binance_symbols WHERE manual=1")}
    now = datetime.now(timezone.utc).isoformat()
    con.executemany(
        "INSERT INTO binance_symbols(symbol,market,base_asset,multiplier,coin_id,status,note,"
        "price_ratio,tracked,resolved_at) VALUES(?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(symbol,market) DO UPDATE SET base_asset=excluded.base_asset,"
        "multiplier=excluded.multiplier,coin_id=excluded.coin_id,status=excluded.status,"
        "note=excluded.note,price_ratio=excluded.price_ratio,tracked=excluded.tracked,"
        "resolved_at=excluded.resolved_at",
        [(*r, int(r[5] == "mapped"), now) for r in rows if (r[0], r[1]) not in manual])
    con.commit()
    return len(manual)


def summary(con):
    counts = {r["status"]: r["n"] for r in
              con.execute("SELECT status, COUNT(*) n FROM binance_symbols GROUP BY status")}
    coins = con.execute("SELECT COUNT(DISTINCT coin_id) n FROM binance_symbols "
                        "WHERE coin_id IS NOT NULL").fetchone()["n"]
    per_market = {r["market"]: r["n"] for r in
                  con.execute("SELECT market, COUNT(*) n FROM binance_symbols "
                              "WHERE status='mapped' GROUP BY market")}
    return {**counts, "coins_covered": coins, "mapped_per_market": per_market}


def ambiguous(con):
    return con.execute("SELECT symbol, market, base_asset, note, price_ratio FROM binance_symbols "
                       "WHERE status='ambiguous' ORDER BY market, symbol").fetchall()


def tracked(con):
    return con.execute("SELECT symbol, market, coin_id FROM binance_symbols "
                       "WHERE tracked=1 ORDER BY market, symbol").fetchall()
