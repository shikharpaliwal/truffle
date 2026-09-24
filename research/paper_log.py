#!/usr/bin/env python
"""Append what each signal fires on TODAY to research/paper_log.jsonl. A LOG ONLY.

No orders, no trading, no recommendation. The backtest universe is today's top 300
by market cap and is survivorship-poisoned; a log accumulated forward from now is the
only unbiased sample. Costs ~300 CoinGecko credits per run (cached within the day),
so run it at most once a day.
"""
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from truffle import config  # noqa: E402
from truffle.cache import Cache  # noqa: E402
from truffle.db import connect  # noqa: E402
from truffle.http import HttpClient  # noqa: E402
from truffle.scoring import rank_scores  # noqa: E402

from research import history, signals as S  # noqa: E402
from research.backtest import S0_TOP_FRAC, W_RELBTC, W_VOL  # noqa: E402

OUT = Path(__file__).resolve().parent / "paper_log.jsonl"
log = logging.getLogger("paper_log")


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    cfg = config.load()
    http = HttpClient(cfg, Cache(cfg["cache_dir"], cfg["cache"]["ttl_seconds"]))
    con = connect(cfg["db_path"])
    ids = [r[0] for r in con.execute(
        "SELECT coin_id FROM universe_snapshots WHERE included=1 AND "
        "run_id=(SELECT MAX(run_id) FROM universe_snapshots) ORDER BY market_cap_rank")]
    history.check_budget(con, cfg, len(ids) + 1)
    hist = history.load(http, ids + ["bitcoin"], days=140)
    history.record_usage(con, http)

    # last UTC day present for BTC, minus the partial day CoinGecko appends
    day = sorted(hist["bitcoin"])[-1]
    feats, btc = {}, None
    for c, h in hist.items():
        dates = sorted(h)
        if dates[-1] != day:
            continue
        p = [h[d][0] for d in dates]
        v = [h[d][1] if h[d][1] is not None else float("nan") for d in dates]
        f = {"s0": S.s0_raw(p, v), "s1": S.s1(p, v), "s2": S.s2(p, v), "s3": S.s3(p, v),
             "ext": S.extension(p), "price": p[-1]}
        if c == "bitcoin":
            btc = f["s0"]
        feats[c] = f

    vol = {c: f["s0"]["vol_mom"] for c, f in feats.items() if f["s0"] and c != "bitcoin"}
    rel = {c: (feats[c]["s0"]["ret_cur"] - btc["ret_cur"])
              - (feats[c]["s0"]["ret_prior"] - btc["ret_prior"]) for c in vol} if btc else {}
    rv, rr = rank_scores(vol), rank_scores(rel)
    comp = {c: (W_VOL * rv[c] + W_RELBTC * rr[c]) / (W_VOL + W_RELBTC)
            for c in vol if rv[c] is not None and rr[c] is not None}
    s0_top = set(sorted(comp, key=comp.get, reverse=True)[:max(1, int(round(len(comp) * S0_TOP_FRAC)))])

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    rows = []
    for c, f in feats.items():
        if c == "bitcoin":
            continue
        nx = S.not_extended(f["ext"])
        s1f, s2f = S.s1_fires(f["s1"]), S.s2_fires(f["s2"])
        fired = [n for n, yes in (("S0_prod_30v30", c in s0_top), ("S1_7v30", s1f),
                                  ("S2_volsurge", s2f), ("S3_accel", S.s3_fires(f["s3"])),
                                  ("S4_S2_notext", s2f and nx),
                                  ("S4b_S1S2_notext", s1f and s2f and nx)) if yes]
        if fired:
            rows.append({"logged_at": now, "as_of": day, "coin_id": c, "price": f["price"],
                         "signals": fired,
                         "z": round(f["s2"]["z"], 2) if f["s2"] else None,
                         "ext": round(f["ext"]["ext"], 3) if f["ext"] else None})
    with OUT.open("a") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")
    log.info("as_of %s: %d firings across %d coins -> %s", day, len(rows), len(feats), OUT)
    for r in sorted(rows, key=lambda r: r["coin_id"]):
        log.info("  %-28s $%-12.6g %s", r["coin_id"], r["price"], ",".join(r["signals"]))


if __name__ == "__main__":
    main()
