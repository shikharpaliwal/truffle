#!/usr/bin/env python
"""Does a short-window breakout signal catch coins as they START moving?

RESEARCH ONLY. Reads data/truffle.db, writes nothing but the cache. Not wired into
the pipeline, the scoring module or the API.

Lookahead control: every signal is evaluated by handing signals.py sequences sliced
as arr[:i+1], so date t is index -1 and no later sample exists in the argument.
Labels are computed in a separate pass that the signal code never sees. `--selftest`
re-computes every feature from a physically truncated copy of the series and asserts
the values are bit-identical.
"""
import argparse
import logging
import sys
from collections import defaultdict
from statistics import median

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))

from truffle import config, universe  # noqa: E402
from truffle.cache import Cache  # noqa: E402
from truffle.db import connect  # noqa: E402
from truffle.http import HttpClient  # noqa: E402
from truffle.scoring import rank_scores  # noqa: E402

from research import execution as X, history, signals as S  # noqa: E402

log = logging.getLogger("backtest")
NAN = float("nan")
WARMUP = 97          # s2 needs 90 baseline days + a 7-day gap
FWD_MAX = 30         # longest forward horizon we must be able to measure
COOLDOWN = 14        # a coin cannot re-fire within this many days (one trade at a time)
S0_TOP_FRAC = 0.10   # production fires = "in today's top decile of the composite"
W_VOL, W_RELBTC = 0.20, 0.15   # config.yaml weights for volume_reputable / rel_btc

TARGETS = {"T1_50pct_14d": (0.50, 14), "T2_30pct_30d": (0.30, 30)}
SIGNALS = ["S0_prod_30v30", "S1_7v30", "S2_volsurge", "S3_accel", "S4_S2_notext", "S4b_S1S2_notext"]


# ------------------------------------------------------------------ point-in-time features
def features_at(p, v, i):
    """All signal inputs for date index i. `p`/`v` are sliced so nothing after i is visible."""
    pp, vv = p[:i + 1], v[:i + 1]
    return {"s0": S.s0_raw(pp, vv), "s1": S.s1(pp, vv), "s2": S.s2(pp, vv),
            "s3": S.s3(pp, vv), "ext": S.extension(pp)}


def build_series(hist, dates):
    """Align a {date: (p,v,m)} map onto the master date axis, NAN where missing."""
    p, v, m = [], [], []
    for d in dates:
        row = hist.get(d)
        p.append(row[0] if row and row[0] is not None else NAN)
        v.append(row[1] if row and row[1] is not None else NAN)
        m.append(row[2] if row and row[2] is not None else NAN)
    return p, v, m


# ------------------------------------------------------------------ labels (future data lives here)
def labels(p, thresh, horizon):
    """hit[i] = a `thresh` peak was available within the next `horizon` days."""
    n = len(p)
    hit, peak = [None] * n, [None] * n
    for i in range(n):
        if p[i] != p[i] or i + horizon >= n:
            continue
        fwd = [x for x in p[i + 1:i + 1 + horizon] if x == x]
        if not fwd:
            continue
        peak[i] = max(fwd) / p[i] - 1.0
        hit[i] = peak[i] >= thresh
    return hit, peak


def terminal(p, horizon):
    n = len(p)
    out = [None] * n
    for i in range(n):
        j = i + horizon
        if j < n and p[i] == p[i] and p[j] == p[j]:
            out[i] = p[j] / p[i] - 1.0
    return out


def move_events(p, hit, lo, hi):
    """Runs of hit==True starting inside [lo,hi]. Each gets a 'liftoff' day.

    liftoff = the first day at/after the run start where price is 10% above the
    run-start price, i.e. when the move is actually visible in the tape.
    """
    ev, i = [], lo
    n = len(p)
    while i <= hi:
        if hit[i] and not (i > 0 and hit[i - 1]):
            j = i
            while j + 1 < n and hit[j + 1]:
                j += 1
            lift = j
            for k in range(i, min(n, j + 15)):
                if p[k] == p[k] and p[i] == p[i] and p[k] >= 1.10 * p[i]:
                    lift = k
                    break
            ev.append({"start": i, "end": j, "liftoff": lift})
            i = j + 1
        else:
            i += 1
    return ev


# ------------------------------------------------------------------ evaluation
def evaluate(coins, feats, series, dates, lo, hi, target):
    """Precision/recall/lead-time for every signal over one universe."""
    thresh, horizon = TARGETS[target]
    lab, ev_by_coin, peak_by_coin, term_by_coin = {}, {}, {}, {}
    for c in coins:
        p = series[c][0]
        hit, peak = labels(p, thresh, horizon)
        lab[c] = hit
        peak_by_coin[c] = peak
        term_by_coin[c] = terminal(p, horizon)
        ev_by_coin[c] = move_events(p, hit, lo, hi)

    base_n = base_hit = 0
    for c in coins:
        for i in range(lo, hi + 1):
            if lab[c][i] is not None:
                base_n += 1
                base_hit += bool(lab[c][i])

    fires = {s: defaultdict(list) for s in SIGNALS}   # signal -> coin -> [indices]
    for c in coins:
        for i in range(lo, hi + 1):
            f = feats[c].get(i)
            if not f:
                continue
            nx = S.not_extended(f["ext"])
            s1f, s2f = S.s1_fires(f["s1"]), S.s2_fires(f["s2"])
            on = {"S0_prod_30v30": f.get("s0_fire", False), "S1_7v30": s1f, "S2_volsurge": s2f,
                  "S3_accel": S.s3_fires(f["s3"]), "S4_S2_notext": s2f and nx,
                  "S4b_S1S2_notext": s1f and s2f and nx}
            for s, yes in on.items():
                if yes:
                    fires[s][c].append(i)

    out = {"base_rate": base_hit / base_n if base_n else 0.0, "coin_days": base_n,
           "n_moves": sum(len(e) for e in ev_by_coin.values()), "signals": {}}

    for s in SIGNALS:
        raw = sum(len(v) for v in fires[s].values())
        events, hits, leads, peaks, terms = [], 0, [], [], []
        for c, idxs in fires[s].items():
            last = -10 ** 9
            for i in idxs:
                if i - last < COOLDOWN:
                    continue
                last = i
                events.append((c, i))
                if lab[c][i]:
                    hits += 1
                    run = next((e for e in ev_by_coin[c] if e["start"] <= i <= e["end"]), None)
                    if run:
                        leads.append(run["liftoff"] - i)
                if peak_by_coin[c][i] is not None:
                    peaks.append(peak_by_coin[c][i])
                if term_by_coin[c][i] is not None:
                    terms.append(term_by_coin[c][i])
        caught = 0
        total_ev = 0
        for c, evs in ev_by_coin.items():
            allf = fires[s].get(c, [])
            for e in evs:
                total_ev += 1
                if any(e["start"] - 7 <= i <= e["start"] + 3 for i in allf):
                    caught += 1
        n = len(events)
        out["signals"][s] = {
            "raw_coin_days": raw, "events": n, "coins": len(fires[s]),
            "precision": hits / n if n else None,
            "lift": (hits / n) / out["base_rate"] if n and out["base_rate"] else None,
            "recall": caught / total_ev if total_ev else None,
            "median_lead": median(leads) if leads else None,
            "pct_lead_ge0": sum(1 for x in leads if x >= 0) / len(leads) if leads else None,
            "median_peak_fwd": median(peaks) if peaks else None,
            "median_term_fwd": median(terms) if terms else None,
        }
    return out, fires, ev_by_coin


# ------------------------------------------------------------------ S0 cross-section
def add_s0_fires(coins, feats, series, dates, lo, hi, btc_p):
    """Rank the production composite across coins each day and fire on the top decile."""
    btc = {}
    for i in range(lo, hi + 1):
        r = S.s0_raw(btc_p[:i + 1], btc_p[:i + 1])
        btc[i] = r
    for i in range(lo, hi + 1):
        b = btc[i]
        vol, rel = {}, {}
        for c in coins:
            f = feats[c].get(i)
            s0 = f and f["s0"]
            if not s0 or not b:
                continue
            vol[c] = s0["vol_mom"]
            rel[c] = (s0["ret_cur"] - b["ret_cur"]) - (s0["ret_prior"] - b["ret_prior"])
        if not vol:
            continue
        rv, rr = rank_scores(vol), rank_scores(rel)
        comp = {c: (W_VOL * rv[c] + W_RELBTC * rr[c]) / (W_VOL + W_RELBTC)
                for c in vol if rv[c] is not None and rr[c] is not None}
        if not comp:
            continue
        k = max(1, int(round(len(comp) * S0_TOP_FRAC)))
        top = sorted(comp, key=comp.get, reverse=True)[:k]
        for c in top:
            feats[c][i]["s0_fire"] = True



# ------------------------------------------------------------------ execution / expectancy
def adv_tiers(uni, series, lo, hi, top=100):
    """Per date, the set of coins in the top `top` by trailing-30d median dollar volume."""
    tiers = {}
    for i in range(lo, hi + 1):
        a = {c: X.adv(series[c][1], i) for c in uni}
        a = {c: x for c, x in a.items() if x}
        tiers[i] = set(sorted(a, key=a.get, reverse=True)[:top])
    return tiers


def trades_for(uni, fires, series, tiers):
    """Cooldown-deduped firings turned into simulated trades, split by liquidity tier."""
    out = {}
    for s in SIGNALS:
        buckets = {"all": [], "liquid": [], "illiquid": []}
        for c in uni:
            last = -10 ** 9
            for i in fires[s].get(c, []):
                if i - last < COOLDOWN:
                    continue
                last = i
                tr = X.simulate(series[c][0], series[c][1], i)
                if not tr:
                    continue
                tr["coin"], tr["i"] = c, i
                buckets["all"].append(tr)
                buckets["liquid" if c in tiers[i] else "illiquid"].append(tr)
        out[s] = {k: X.summarise(v) for k, v in buckets.items()}
        out[s]["_raw"] = buckets
    return out


def report_exec(tt, title):
    print(f"\n{title}")
    print(f"  entry = next daily snapshot after the signal; exit = stop -{X.STOP:.0%} "
          f"(filled at the observed price, gaps included) / TP +{X.TP:.0%} (limit) / "
          f"{X.HOLD}d time-exit; fee {X.FEE:.2%}/side; position ${X.NOTIONAL:,.0f}")
    hdr = (f"  {'signal':<17}{'tier':<10}{'n':>5}{'stop%':>7}{'tp%':>6}{'gross':>8}"
           f"{'e@0.3%':>8}{'e@1%':>8}{'e@3%':>8}{'e@vol':>8}{'win@1%':>8}{'BEslip':>8}")
    print(hdr)
    for s in SIGNALS:
        for tier in ("all", "liquid", "illiquid"):
            r = tt[s][tier]
            if not r:
                continue
            be = "   n/a" if r["breakeven_slip"] is None else f"{100 * r['breakeven_slip']:+.2f}%"
            print(f"  {s:<17}{tier:<10}{r['n']:>5}{pct(r['pct_stop']):>7}{pct(r['pct_tp']):>6}"
                  f"{pct(r['gross_mean']):>8}{pct(r['exp_0.003']):>8}{pct(r['exp_0.01']):>8}"
                  f"{pct(r['exp_0.03']):>8}{pct(r['exp_vol']):>8}{pct(r['win_0.01']):>8}{be:>8}")
    print(f"\n  {'signal':<17}{'tier':<10}{'n':>5}{'medMDD':>8}{'worstMDD':>10}{'p10@1%':>8}"
          f"{'med@1%':>8}{'gap>2%':>8}{'medGap':>8}{'medPart':>9}{'noExit%':>9}")
    for s in SIGNALS:
        for tier in ("all", "liquid", "illiquid"):
            r = tt[s][tier]
            if not r:
                continue
            mp = "  n/a" if r["median_part"] is None else f"{100 * r['median_part']:.2f}%"
            print(f"  {s:<17}{tier:<10}{r['n']:>5}{pct(r['median_mdd']):>8}{pct(r['worst_mdd']):>10}"
                  f"{pct(r['p10_0.01']):>8}{pct(r['med_0.01']):>8}{pct(r['pct_gap_2pct']):>8}"
                  f"{pct(r['median_gap_on_stops']):>8}{mp:>9}{pct(r['pct_not_exitable']):>9}")


# ------------------------------------------------------------------ reporting
def pct(x):
    return "   n/a" if x is None else f"{100 * x:5.1f}%"


def report_table(res, title):
    print(f"\n{title}")
    print(f"  coin-days={res['coin_days']}  base rate={pct(res['base_rate'])}  "
          f"distinct moves={res['n_moves']}")
    print(f"  {'signal':<17}{'fires':>7}{'coins':>7}{'prec':>8}{'lift':>7}{'recall':>8}"
          f"{'lead':>7}{'early':>8}{'medPeak':>9}{'medHold':>9}")
    for s in SIGNALS:
        r = res["signals"][s]
        lead = "  n/a" if r["median_lead"] is None else f"{r['median_lead']:+.0f}d"
        lift = "  n/a" if r["lift"] is None else f"{r['lift']:.2f}x"
        print(f"  {s:<17}{r['events']:>7}{r['coins']:>7}{pct(r['precision']):>8}{lift:>7}"
              f"{pct(r['recall']):>8}{lead:>7}{pct(r['pct_lead_ge0']):>8}"
              f"{pct(r['median_peak_fwd']):>9}{pct(r['median_term_fwd']):>9}")


def selftest(series, dates, lo, hi):
    """Prove no lookahead: features from a physically truncated series must match."""
    import random
    random.seed(7)
    cs = random.sample(list(series), min(12, len(series)))
    checked = 0
    for c in cs:
        p, v, _ = series[c]
        for i in random.sample(range(lo, hi + 1), 12):
            full = features_at(p, v, i)
            trunc = features_at(p[:i + 1], v[:i + 1], i)   # future physically absent
            assert full == trunc, f"LOOKAHEAD at {c} {dates[i]}: {full} != {trunc}"
            checked += 1
    print(f"selftest: {checked} (coin,date) feature vectors identical when the future is "
          f"deleted from memory -- no lookahead in the signal path.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pool", type=int, default=600, help="today's top-N to pull history for")
    ap.add_argument("--size", type=int, default=300, help="universe size per test")
    ap.add_argument("--force", action="store_true", help="ignore the monthly budget")
    ap.add_argument("--dry-run", action="store_true", help="print the cost projection and stop")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    cfg = config.load()
    cache = Cache(cfg["cache_dir"], None)        # research cache never expires
    http = HttpClient(cfg, cache)
    con = connect(cfg["db_path"])

    proj = 5 + len(cfg["exclusions"]["categories"]) + args.pool + 1
    history.check_budget(con, cfg, proj, args.force)
    if args.dry_run:
        return
    kept, _ = universe.build(http, cfg, args.pool)
    ids = [r["id"] for r in kept]
    if "bitcoin" not in ids:
        ids.append("bitcoin")
    if "mina-protocol" not in ids:
        ids.append("mina-protocol")
    log.info("loading 365d history for %d coins", len(ids))
    hist = history.load(http, ids)
    history.record_usage(con, http)
    log.info("http: %s", http.stats())

    dates = sorted(hist["bitcoin"])
    series = {c: build_series(h, dates) for c, h in hist.items()}
    btc_p = series["bitcoin"][0]
    lo, hi = WARMUP, len(dates) - 1 - FWD_MAX
    rank_now = {r["id"]: n for n, r in enumerate(kept, 1)}

    print("=" * 96)
    print(f"data: {len(hist)} coins, {dates[0]} .. {dates[-1]} ({len(dates)} days)")
    print(f"test dates: {dates[lo]} .. {dates[hi]} ({hi - lo + 1} days)")

    all_coins = [c for c in series if c != "bitcoin"]
    log.info("computing point-in-time features for %d coins x %d days", len(all_coins), hi - lo + 1)
    feats = {}
    for c in all_coins:
        p, v, _ = series[c]
        feats[c] = {i: features_at(p, v, i) for i in range(lo, hi + 1)}

    selftest(series, dates, lo, hi)

    # universe A: today's top-`size` (survivorship-contaminated, what data/truffle.db holds)
    uni_today = [c for c in sorted(all_coins, key=lambda c: rank_now.get(c, 10 ** 6))][:args.size]
    # universe B: reconstructed -- top `size` by market cap ON THE TEST START DATE
    mc0 = {c: series[c][2][lo] for c in all_coins if series[c][2][lo] == series[c][2][lo]}
    uni_asof = sorted(mc0, key=mc0.get, reverse=True)[:args.size]

    add_s0_fires(all_coins, feats, series, dates, lo, hi, btc_p)

    results = {}
    for uname, uni in (("A_today_top300", uni_today), ("B_asof_start_top300", uni_asof)):
        for t in TARGETS:
            res, fires, evs = evaluate(uni, feats, series, dates, lo, hi, t)
            results[(uname, t)] = (res, fires)
            report_table(res, f"[{uname}] target {t} (peak +{TARGETS[t][0]:.0%} within "
                              f"{TARGETS[t][1]}d)  n_coins={len(uni)}")

    # ---------------- execution / expectancy
    print("\n" + "=" * 96)
    print("EXECUTION MODEL  (firings are cooldown-deduped: one open trade per coin)")
    for uname, uni in (("A_today_top300", uni_today), ("B_asof_start_top300", uni_asof)):
        tiers = adv_tiers(uni, series, lo, hi)
        tt = trades_for(uni, results[(uname, "T1_50pct_14d")][1], series, tiers)
        report_exec(tt, f"[{uname}]  liquid = coin in the day's top 100 by trailing-30d "
                        f"dollar volume")

    # ---------------- survivorship
    overlap = len(set(uni_today) & set(uni_asof))
    grew = sum(1 for c in uni_today
               if series[c][2][lo] == series[c][2][lo] and series[c][2][hi] == series[c][2][hi]
               and series[c][2][hi] > 3 * series[c][2][lo])
    print("\n" + "=" * 96)
    print("SURVIVORSHIP")
    print(f"  pool pulled today: top {args.pool} by market cap (post-exclusions)")
    print(f"  universe A (today's top {args.size}) vs B (top {args.size} by mcap on {dates[lo]}): "
          f"{overlap} shared, {args.size - overlap} differ")
    print(f"  coins in A whose market cap more than tripled over the sample: {grew} "
          f"({grew / len(uni_today):.0%}) -- these are in A only BECAUSE they pumped")
    for t in TARGETS:
        a = results[("A_today_top300", t)][0]
        b = results[("B_asof_start_top300", t)][0]
        print(f"  {t}: base rate A={pct(a['base_rate'])} vs B={pct(b['base_rate'])}")
        for s in SIGNALS:
            pa, pb = a["signals"][s]["precision"], b["signals"][s]["precision"]
            if pa is not None and pb is not None:
                print(f"     {s:<17} precision A={pct(pa)}  B={pct(pb)}  "
                      f"delta={100 * (pa - pb):+.1f}pp")

    # ---------------- Mina
    print("\n" + "=" * 96)
    print("MINA CHECK (mina-protocol)")
    if "mina-protocol" not in feats:
        print("  no history")
    else:
        p = series["mina-protocol"][0]
        fires = results[("A_today_top300", "T1_50pct_14d")][1]
        for d in ("2026-08-10", "2026-08-15", "2026-08-18", "2026-08-20", "2026-08-22",
                  "2026-09-01", "2026-09-10", "2026-09-23"):
            if d in dates:
                print(f"    price {d}: ${p[dates.index(d)]:.4f}")
        for s in SIGNALS:
            idxs = fires.get(s, {}).get("mina-protocol", [])
            if not idxs:
                print(f"  {s:<17} NEVER fired")
                continue
            shown = [f"{dates[i]}@${p[i]:.4f}" for i in idxs]
            win = [dates[i] for i in idxs if "2026-08-19" <= dates[i] <= "2026-08-22"]
            print(f"  {s:<17} {len(idxs)} firings: {', '.join(shown[:14])}"
                  f"{' ...' if len(shown) > 14 else ''}")
            print(f"  {'':17} 19-22 Aug window: {win or 'NOTHING'}")
    print("=" * 96)


if __name__ == "__main__":
    main()
