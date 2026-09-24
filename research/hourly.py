#!/usr/bin/env python
"""Does a short-window breakout signal work on REAL hourly Binance bars?

RESEARCH ONLY. Reads data/truffle.db read-only (klines_1h + binance_symbols) and the
CoinGecko disk cache (for the as-of-start market caps). Writes nothing, touches no
pipeline/scoring/API code, places no orders.

What this adds over research/backtest.py (the daily run):
  * real OHLC, so stops are checked against the actual hourly high/low with
    gap-through fills -- the daily run could only see one close per day and therefore
    biased stop frequency DOWNWARD.
  * real exchange quote volume for the liquidity tiering, not CoinGecko's aggregate.

Signal DEFINITIONS and THRESHOLDS are imported verbatim from research/signals.py --
nothing is re-tuned here. Only the window LENGTHS are re-expressed in hours, under two
parameterisations fixed before any result was looked at (see WINDOWS).

Lookahead control is the daily run's: features are computed from arr[:i+1] with negative
indexing only, labels live in a separate pass, and --selftest re-derives every feature
from a physically truncated copy and asserts bit-identity.
"""
import argparse
import glob
import json
import math
import re
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timezone
from statistics import median, pstdev

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))

from truffle.scoring import rank_scores  # noqa: E402

from research import execution as X  # noqa: E402
from research import signals as S  # noqa: E402

NAN = float("nan")
HOUR_MS = 3_600_000
DAY_MS = 86_400_000

# ---------------------------------------------------------------- a-priori parameters
# Calendar-anchored and identical to the daily run, so the two tables are comparable.
TARGETS = {"T1_50pct_14d": (0.50, 336), "T2_30pct_30d": (0.30, 720)}
FWD_MAX = 720        # longest forward horizon, in hours
COOLDOWN = 336       # one open trade per coin, 14d, as daily
HOLD = 336           # 14d holding cap, as daily
S0_TOP_FRAC = 0.10
W_VOL, W_RELBTC = 0.20, 0.15
VOL10X_MULT, VOL10X_LOOK = 10.0, 168   # the feasibility study's Mina rule

# Two window parameterisations, both chosen before looking at any output.
#   P1  bar-for-bar: each daily lookback of N bars becomes N HOURLY bars. This is the
#       short-window/high-resolution regime the Mina "+36h lead" claim lives in.
#   P2  calendar-scaled: 7d -> 168h, 30d -> 720h. S2's 90d baseline CANNOT be scaled
#       (2160h + 168h gap exceeds the entire 2160-bar sample), so it is capped at 720h;
#       that deviation is forced by the sample length, not chosen for results.
WINDOWS = {
    "P1_bar_for_bar": dict(s1_short=7, s1_long=30, s2_recent=3, s2_base=90, s2_gap=7,
                           s3_k=7, ext_look=60, s0_w=30),
    "P2_calendar": dict(s1_short=168, s1_long=720, s2_recent=72, s2_base=720, s2_gap=168,
                        s3_k=168, ext_look=720, s0_w=720),
    # P3 is the longest calendar-scaled set the 90-day sample can actually carry:
    # 1d vs 14d windows, a 14d volume baseline, a 7d composite. Also fixed a priori.
    "P3_intraday_14d": dict(s1_short=24, s1_long=336, s2_recent=6, s2_base=336, s2_gap=24,
                            s3_k=24, ext_look=336, s0_w=168),
}
SIGNALS = ["BASELINE_always", "S0_prod", "S1_short_v_long", "S2_volsurge", "S3_accel",
           "S4_S2_notext", "S4b_S1S2_notext", "S5_vol10x"]


# ---------------------------------------------------------------- point-in-time signals
# Window lengths are parameters; thresholds come straight from research/signals.py.
def s1(p, v, short, long):
    pw, pl, vw, vl = S._win(p, short), S._win(p, long), S._win(v, short), S._win(v, long)
    if pw is None or pl is None or vw is None or vl is None or S._mean(pl) <= 0 or S._mean(vl) <= 0:
        return None
    return {"price_ratio": S._mean(pw) / S._mean(pl), "vol_ratio": S._mean(vw) / S._mean(vl)}


def s2(v, recent, base, gap):
    r, b = S._win(v, recent), S._win(v, base, gap)
    if r is None or b is None:
        return None
    sd = pstdev(b)
    if sd <= 0:
        return None
    return {"z": (S._mean(r) - S._mean(b)) / sd}


def s3(p, k):
    now, mid, old = S._at(p, 0), S._at(p, k), S._at(p, 2 * k)
    if now is None or not mid or not old:
        return None
    r1, r0 = now / mid - 1.0, mid / old - 1.0
    return {"accel": r1 - r0, "ret": r1}


def extension(p, look):
    w = S._win(p, look)
    m = median(w) if w else None
    now = S._at(p, 0)
    return None if not m or now is None else {"ext": now / m}


def s0_raw(p, v, w):
    return S.s0_raw(p, v, w)


def vol10x(v, look=VOL10X_LOOK):
    """The feasibility study's rule: this hour's volume vs the trailing median."""
    b = S._win(v, look, 1)
    now = S._at(v, 0)
    if b is None or now is None:
        return None
    m = median(b)
    return None if m <= 0 else {"mult": now / m}


def vol10x_fires(f):
    return bool(f) and f["mult"] >= VOL10X_MULT


def features_at(p, v, i, W):
    pp, vv = p[:i + 1], v[:i + 1]
    return {"s0": s0_raw(pp, vv, W["s0_w"]),
            "s1": s1(pp, vv, W["s1_short"], W["s1_long"]),
            "s2": s2(vv, W["s2_recent"], W["s2_base"], W["s2_gap"]),
            "s3": s3(pp, W["s3_k"]),
            "ext": extension(pp, W["ext_look"]),
            "v10": vol10x(vv)}


def warmup(W):
    return max(2 * W["s1_long"], W["s2_base"] + W["s2_gap"], 2 * W["s3_k"],
               W["ext_look"], 2 * W["s0_w"], VOL10X_LOOK + 1) + 1


# ---------------------------------------------------------------- data
def load_bars(db):
    """{coin_id: (times, o, h, l, c, qv)} on the master hourly axis. Spot preferred."""
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    mapped = {}
    for r in con.execute("SELECT symbol, market, coin_id FROM binance_symbols "
                         "WHERE status='mapped' AND tracked=1"):
        # spot is the venue a retail taker would actually hit, and its quote volume is
        # genuine exchange turnover; perps are only used where no spot pair exists.
        cur = mapped.get(r["coin_id"])
        if cur is None or (cur[1] == "perp" and r["market"] == "spot"):
            mapped[r["coin_id"]] = (r["symbol"], r["market"])
    times = [t for (t,) in con.execute("SELECT DISTINCT open_time FROM klines_1h ORDER BY open_time")]
    idx = {t: i for i, t in enumerate(times)}
    n = len(times)
    out = {}
    for cid, (sym, mkt) in mapped.items():
        o, h, l, c, qv = ([NAN] * n for _ in range(5))
        for r in con.execute("SELECT open_time,open,high,low,close,quote_volume FROM klines_1h "
                             "WHERE symbol=? AND market=?", (sym, mkt)):
            i = idx[r["open_time"]]
            o[i], h[i], l[i], c[i], qv[i] = (r["open"], r["high"], r["low"], r["close"],
                                             r["quote_volume"])
        out[cid] = (o, h, l, c, qv, sym, mkt)
    con.close()
    return times, out


def asof_universe(cache_dir, asof_date, size=300):
    """Top `size` coins by market cap on `asof_date`, from the daily study's 600-coin pool.

    Read straight out of the cached CoinGecko 365d market_chart bodies -- no HTTP, no
    credits. This is option (a): an as-of-START universe, not today's winners.
    """
    mc = {}
    for f in glob.glob(f"{cache_dir}/coingecko/*.json"):
        try:
            rec = json.load(open(f))
        except (json.JSONDecodeError, OSError):
            continue
        k = rec.get("key", "")
        if "market_chart" not in k or "('days', 365)" not in k or "interval" in k:
            continue
        cid = re.search(r"/coins/([^/]+)/market_chart", k).group(1)
        body = rec.get("body") or {}
        for ts, val in (body.get("market_caps") or []):
            if ts % DAY_MS == 0 and val:
                d = datetime.fromtimestamp(ts / 1000, timezone.utc).strftime("%Y-%m-%d")
                if d == asof_date:
                    mc[cid] = val
    return set(sorted(mc, key=mc.get, reverse=True)[:size]), len(mc)


# ---------------------------------------------------------------- labels (future lives here)
_LABEL_CACHE = {}


def labels(p, thresh, horizon, key=None):
    """hit[i] = a `thresh` peak was reachable within the next `horizon` bars.

    Sliding-window max, so the same values the naive O(n*h) loop would give.
    """
    if key is not None and key in _LABEL_CACHE:
        return _LABEL_CACHE[key]
    from collections import deque
    n = len(p)
    hit, peak, dq = [None] * n, [None] * n, deque()
    for i in range(n - 1, -1, -1):
        j = i + 1
        if j < n and p[j] == p[j]:
            while dq and p[dq[-1]] <= p[j]:
                dq.pop()
            dq.append(j)
        while dq and dq[0] > i + horizon:
            dq.popleft()
        if p[i] != p[i] or i + horizon >= n or not dq:
            continue
        peak[i] = p[dq[0]] / p[i] - 1.0
        hit[i] = peak[i] >= thresh
    if key is not None:
        _LABEL_CACHE[key] = (hit, peak)
    return hit, peak


_TERM_CACHE, _EV_CACHE = {}, {}


def _cached(d, k, fn):
    if k not in d:
        d[k] = fn()
    return d[k]


def terminal(p, horizon):
    n = len(p)
    out = [None] * n
    for i in range(n):
        j = i + horizon
        if j < n and p[i] == p[i] and p[j] == p[j]:
            out[i] = p[j] / p[i] - 1.0
    return out


def move_events(p, hit, lo, hi):
    ev, i, n = [], lo, len(p)
    while i <= hi:
        if hit[i] and not (i > 0 and hit[i - 1]):
            j = i
            while j + 1 < n and hit[j + 1]:
                j += 1
            lift = j
            for k in range(i, min(n, j + 15 * 24)):
                if p[k] == p[k] and p[i] == p[i] and p[k] >= 1.10 * p[i]:
                    lift = k
                    break
            ev.append({"start": i, "end": j, "liftoff": lift})
            i = j + 1
        else:
            i += 1
    return ev


# ---------------------------------------------------------------- execution on real OHLC
def adv(qv, i, days=30):
    """Trailing median 24h dollar volume ending at bar i, point-in-time."""
    sums = []
    for d in range(days):
        hi = i + 1 - 24 * d
        lo = hi - 24
        if lo < 0:
            break
        w = [x for x in qv[lo:hi] if x == x]
        if len(w) == 24:
            sums.append(sum(w))
    return median(sums) if sums else None


def simulate(o, h, l, c, qv, t):
    """One trade from a firing at bar t, against the actual hourly path.

    Entry: the NEXT bar's OPEN. Exit, first to happen within HOLD hours:
      stop at -20%  -- filled at the bar OPEN when the bar gapped through it, else at
                       the stop level (so gap risk is charged, never assumed away);
      TP at +40%    -- filled at the limit, or at the open if the bar gapped above it;
      else time-exit at the close of the last bar.
    A bar that touches both levels is resolved as a STOP (conservative).
    """
    e = t + 1
    n = len(o)
    if e + 1 >= n or o[e] != o[e] or o[e] <= 0:
        return None
    ref = o[e]
    stop_lv, tp_lv = ref * (1 - X.STOP), ref * (1 + X.TP)
    last = min(n - 1, e + HOLD)
    px, why, xi, gap, both = c[last], "time", last, 0.0, 0
    lo_seen = ref
    for d in range(e + 1, last + 1):
        if l[d] != l[d]:
            continue
        lo_seen = min(lo_seen, l[d])
        hit_s, hit_t = l[d] <= stop_lv, h[d] >= tp_lv
        if hit_s and hit_t:
            both += 1
        if hit_s:
            fill = min(o[d], stop_lv)
            px, why, xi = fill, "stop", d
            gap = stop_lv / fill - 1.0
            break
        if hit_t:
            px, why, xi = max(o[d], tp_lv), "tp", d
            break
    if px != px:
        return None
    a_in, a_out = adv(qv, e), adv(qv, xi)
    return {"entry_i": e, "exit_i": xi, "why": why, "gross": px / ref - 1.0,
            "gap": gap, "mdd": lo_seen / ref - 1.0, "days": (xi - e) / 24.0,
            "hours": xi - e, "both": both,
            "adv_in": a_in, "adv_out": a_out,
            "part": (X.NOTIONAL / a_in) if a_in else None,
            "slip_in": X.vol_slip(a_in), "slip_out": X.vol_slip(a_out)}


# ---------------------------------------------------------------- evaluation
def fire_map(coins, feats, lo, hi):
    fires = {s: defaultdict(list) for s in SIGNALS}
    for c in coins:
        for i in range(lo, hi + 1):
            f = feats[c].get(i)
            if not f:
                continue
            nx = S.not_extended(f["ext"])
            s1f, s2f = S.s1_fires(f["s1"]), S.s2_fires(f["s2"])
            on = {"S0_prod": f.get("s0_fire", False), "S1_short_v_long": s1f,
                  "S2_volsurge": s2f, "S3_accel": S.s3_fires(f["s3"]),
                  "S4_S2_notext": s2f and nx, "S4b_S1S2_notext": s1f and s2f and nx,
                  "S5_vol10x": vol10x_fires(f["v10"]),
                  # unconditional control: buy every coin every COOLDOWN hours. Any signal
                  # whose expectancy does not beat this has no edge, only market beta.
                  "BASELINE_always": True}
            for s, yes in on.items():
                if yes:
                    fires[s][c].append(i)
    return fires


def wilson(k, n, z=1.96):
    if not n:
        return None
    p = k / n
    d = 1 + z * z / n
    ctr = (p + z * z / (2 * n)) / d
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, ctr - half), min(1.0, ctr + half))


def evaluate(coins, feats, series, fires, lo, hi, target):
    thresh, horizon = TARGETS[target]
    lab, ev_by_coin, peak_by_coin, term_by_coin = {}, {}, {}, {}
    for c in coins:
        p = series[c][3]
        hit, peak = labels(p, thresh, horizon, key=(c, target))
        lab[c], peak_by_coin[c] = hit, peak
        term_by_coin[c] = _cached(_TERM_CACHE, (c, target), lambda: terminal(p, horizon))
        ev_by_coin[c] = _cached(_EV_CACHE, (c, target, lo, hi),
                                lambda: move_events(p, hit, lo, hi))

    base_n = base_hit = 0
    for c in coins:
        for i in range(lo, hi + 1):
            if lab[c][i] is not None:
                base_n += 1
                base_hit += bool(lab[c][i])

    out = {"base_rate": base_hit / base_n if base_n else 0.0, "coin_bars": base_n,
           "n_moves": sum(len(e) for e in ev_by_coin.values()), "signals": {}}

    for s in SIGNALS:
        raw = sum(len(fires[s].get(c, [])) for c in coins)
        events, hits, leads, peaks, terms = [], 0, [], [], []
        for c in coins:
            last = -10 ** 9
            for i in fires[s].get(c, []):
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
        caught = total_ev = 0
        for c in coins:
            allf = fires[s].get(c, [])
            for e in ev_by_coin[c]:
                total_ev += 1
                if any(e["start"] - 168 <= i <= e["start"] + 72 for i in allf):
                    caught += 1
        n = len(events)
        ci = wilson(hits, n) if n else None
        out["signals"][s] = {
            "raw_coin_bars": raw, "events": n, "coins": sum(1 for c in coins if fires[s].get(c)),
            "precision": hits / n if n else None,
            "se": math.sqrt((hits / n) * (1 - hits / n) / n) if n else None,
            "ci": ci,
            "lift": (hits / n) / out["base_rate"] if n and out["base_rate"] else None,
            "recall": caught / total_ev if total_ev else None,
            "median_lead_h": median(leads) if leads else None,
            "pct_lead_ge0": sum(1 for x in leads if x >= 0) / len(leads) if leads else None,
            "median_peak_fwd": median(peaks) if peaks else None,
            "median_term_fwd": median(terms) if terms else None,
        }
    return out


def add_s0_fires(coins, feats, lo, hi, btc_p, w):
    btc = {i: S.s0_raw(btc_p[:i + 1], btc_p[:i + 1], w) for i in range(lo, hi + 1)}
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
        for c in sorted(comp, key=comp.get, reverse=True)[:k]:
            feats[c][i]["s0_fire"] = True


def tiers_by_bar(uni, series, lo, hi, top=50, step=24):
    """Top-`top` by trailing 30d median 24h quote volume, refreshed daily."""
    marks = list(range(lo, hi + 1, step))
    t = {}
    for i in marks:
        a = {c: adv(series[c][4], i) for c in uni}
        a = {c: x for c, x in a.items() if x}
        t[i] = set(sorted(a, key=a.get, reverse=True)[:top])
    def at(i):
        m = max(x for x in marks if x <= i)
        return t[m]
    return at


def trades_for(uni, fires, series, tier_at):
    out = {}
    for s in SIGNALS:
        buckets = {"all": [], "liquid": [], "illiquid": []}
        for c in uni:
            last = -10 ** 9
            for i in fires[s].get(c, []):
                if i - last < COOLDOWN:
                    continue
                last = i
                o, h, l, cl, qv = series[c][:5]
                tr = simulate(o, h, l, cl, qv, i)
                if not tr:
                    continue
                tr["coin"], tr["i"] = c, i
                buckets["all"].append(tr)
                buckets["liquid" if c in tier_at(i) else "illiquid"].append(tr)
        out[s] = {k: X.summarise(v) for k, v in buckets.items()}
        out[s]["_raw"] = buckets
    return out


# ---------------------------------------------------------------- reporting
def pct(x):
    return "   n/a" if x is None else f"{100 * x:5.1f}%"


def report_table(res, title):
    print(f"\n{title}")
    print(f"  coin-bars={res['coin_bars']}  base rate={pct(res['base_rate'])}  "
          f"distinct moves={res['n_moves']}")
    print(f"  {'signal':<17}{'fires':>7}{'coins':>7}{'prec':>8}{'+-SE':>7}{'95% CI':>15}"
          f"{'lift':>7}{'recall':>8}{'lead':>8}{'early':>8}{'medPeak':>9}{'medHold':>9}")
    for s in SIGNALS:
        r = res["signals"][s]
        lead = "   n/a" if r["median_lead_h"] is None else f"{r['median_lead_h']:+.0f}h"
        lift = "  n/a" if r["lift"] is None else f"{r['lift']:.2f}x"
        se = "  n/a" if r["se"] is None else f"{100 * r['se']:4.1f}"
        ci = "        n/a" if not r["ci"] else f"[{100*r['ci'][0]:4.1f},{100*r['ci'][1]:5.1f}]"
        print(f"  {s:<17}{r['events']:>7}{r['coins']:>7}{pct(r['precision']):>8}{se:>7}{ci:>15}"
              f"{lift:>7}{pct(r['recall']):>8}{lead:>8}{pct(r['pct_lead_ge0']):>8}"
              f"{pct(r['median_peak_fwd']):>9}{pct(r['median_term_fwd']):>9}")


def report_exec(tt, title):
    print(f"\n{title}")
    print(f"  entry = NEXT hourly bar's open; exit = stop -{X.STOP:.0%} (filled at the bar "
          f"open when it gapped through) / TP +{X.TP:.0%} / {HOLD}h time-exit; "
          f"fee {X.FEE:.2%}/side; position ${X.NOTIONAL:,.0f}")
    print(f"  {'signal':<17}{'tier':<10}{'n':>5}{'stop%':>7}{'tp%':>6}{'gross':>8}"
          f"{'e@0%':>8}{'e@0.3%':>8}{'e@1%':>8}{'e@3%':>8}{'e@vol':>8}{'win@1%':>8}{'BEslip':>8}")
    for s in SIGNALS:
        for tier in ("all", "liquid", "illiquid"):
            r = tt[s][tier]
            if not r:
                continue
            be = "   n/a" if r["breakeven_slip"] is None else f"{100 * r['breakeven_slip']:+.2f}%"
            e0 = sum(X.net(t, 0.0, 0.0) for t in tt[s]["_raw"][tier]) / r["n"]
            print(f"  {s:<17}{tier:<10}{r['n']:>5}{pct(r['pct_stop']):>7}{pct(r['pct_tp']):>6}"
                  f"{pct(r['gross_mean']):>8}{pct(e0):>8}{pct(r['exp_0.003']):>8}"
                  f"{pct(r['exp_0.01']):>8}{pct(r['exp_0.03']):>8}{pct(r['exp_vol']):>8}"
                  f"{pct(r['win_0.01']):>8}{be:>8}")
    print(f"\n  {'signal':<17}{'tier':<10}{'n':>5}{'medH':>7}{'medMDD':>8}{'worstMDD':>10}"
          f"{'p10@1%':>8}{'med@1%':>8}{'gap>2%':>8}{'medGap':>8}{'medPart':>9}{'noExit%':>9}")
    for s in SIGNALS:
        for tier in ("all", "liquid", "illiquid"):
            r = tt[s][tier]
            if not r:
                continue
            mp = "  n/a" if r["median_part"] is None else f"{100 * r['median_part']:.2f}%"
            mh = median(t["hours"] for t in tt[s]["_raw"][tier])
            print(f"  {s:<17}{tier:<10}{r['n']:>5}{mh:>7.0f}{pct(r['median_mdd']):>8}"
                  f"{pct(r['worst_mdd']):>10}{pct(r['p10_0.01']):>8}{pct(r['med_0.01']):>8}"
                  f"{pct(r['pct_gap_2pct']):>8}{pct(r['median_gap_on_stops']):>8}"
                  f"{mp:>9}{pct(r['pct_not_exitable']):>9}")


def resolution_check(uni, fires, series, times, lo, hi):
    """Same firings, same window, two resolutions -- so any difference IS the resolution.

    Hourly: stops checked against the real hourly low. Daily-eyes: the daily study's
    model, which can only observe one close per UTC day and so cannot see an intraday
    stop-out at all. If the daily study understated stops, it shows up here.
    """
    day_idx = [i for i in range(len(times)) if times[i] % DAY_MS == 0]
    pos = {b: k for k, b in enumerate(day_idx)}
    print("\n" + "-" * 118)
    print("RESOLUTION CHECK -- identical firings priced on hourly OHLC vs daily closes only")
    print(f"  {'signal':<17}{'n':>5}{'stopH':>8}{'stopD':>8}{'grossH':>9}{'grossD':>9}"
          f"{'e@1%H':>9}{'e@1%D':>9}{'gap>2%H':>9}{'gap>2%D':>9}")
    for s in SIGNALS:
        h_tr, d_tr = [], []
        for c in uni:
            o, hh, ll, cl, qv = series[c][:5]
            dc = [cl[b] for b in day_idx]
            dv = [sum(x for x in qv[b - 23:b + 1] if x == x) for b in day_idx]
            last = -10 ** 9
            for i in fires[s].get(c, []):
                if i - last < COOLDOWN:
                    continue
                last = i
                t = simulate(o, hh, ll, cl, qv, i)
                nd = next((k for b, k in pos.items() if b >= i), None)
                d = X.simulate(dc, dv, nd - 1) if nd else None
                if t and d:
                    h_tr.append(t)
                    d_tr.append(d)
        if not h_tr:
            continue
        n = len(h_tr)
        f = lambda tr, k: sum(1 for t in tr if t["why"] == "stop") / len(tr) if k == "s" else None
        gh = sum(t["gross"] for t in h_tr) / n
        gd = sum(t["gross"] for t in d_tr) / n
        eh = sum(X.net(t, 0.01, 0.01) for t in h_tr) / n
        ed = sum(X.net(t, 0.01, 0.01) for t in d_tr) / n
        print(f"  {s:<17}{n:>5}{pct(f(h_tr,'s')):>8}{pct(f(d_tr,'s')):>8}{pct(gh):>9}{pct(gd):>9}"
              f"{pct(eh):>9}{pct(ed):>9}"
              f"{pct(sum(1 for t in h_tr if t['gap'] > 0.02) / n):>9}"
              f"{pct(sum(1 for t in d_tr if t['gap'] > 0.02) / n):>9}")


def regime(series, times, uni):
    """What the 90-day sample actually did, so 'positive expectancy' can be read in context."""
    rets = []
    for c in uni:
        cl = series[c][3]
        a = next((x for x in cl if x == x), None)
        b = next((x for x in reversed(cl) if x == x), None)
        if a and b:
            rets.append(b / a - 1.0)
    btc = series["bitcoin"][3]
    print("\n" + "-" * 118)
    print("REGIME -- the sample is a single 90-day window, and this is what it did")
    print(f"  BTC over the window: {100 * (btc[-1] / btc[0] - 1):+.1f}%")
    print(f"  altcoin buy-and-hold: median {100 * median(rets):+.1f}%, "
          f"mean {100 * sum(rets) / len(rets):+.1f}%, "
          f"{100 * sum(1 for r in rets if r > 0) / len(rets):.0f}% of coins up")


def _block_boot(sig, base, draws=2000, seed=11):
    """95% CI on the edge, resampling whole ENTRY DAYS.

    Trades opened on the same day share one market move, so they are one observation,
    not many. Ignoring that is what makes a one-regime sample look significant.
    """
    import random
    rng = random.Random(seed)
    by_day = defaultdict(lambda: ([], []))
    for t in sig:
        by_day[t["entry_i"] // 24][0].append(X.net(t, 0.01, 0.01))
    for t in base:
        by_day[t["entry_i"] // 24][1].append(X.net(t, 0.01, 0.01))
    days = list(by_day)
    if len(days) < 3:
        return None, None
    out = []
    for _ in range(draws):
        pick = [by_day[rng.choice(days)] for _ in days]
        a = [x for p_ in pick for x in p_[0]]
        b = [x for p_ in pick for x in p_[1]]
        if a and b:
            out.append(sum(a) / len(a) - sum(b) / len(b))
    out.sort()
    return out[int(0.025 * len(out))], out[int(0.975 * len(out)) - 1]


def edge_vs_baseline(tt, title):
    """Signal expectancy MINUS the unconditional baseline, with a Welch t-test.

    In a rising sample every entry makes money, so raw expectancy is beta, not edge.
    The only number that matters is whether a signal beats buying at random.
    """
    base = {k: [X.net(t, 0.01, 0.01) for t in tt["BASELINE_always"]["_raw"][k]]
            for k in ("all", "liquid", "illiquid")}
    print(f"\n  {title}")
    print("  (t/p assume independent trades, which overlapping 336h holds in one regime are"
          " NOT;\n   the day-block bootstrap CI resamples whole ENTRY DAYS and is the honest one)")
    print(f"  {'signal':<17}{'tier':<10}{'n':>5}{'e@1%':>8}{'base':>8}{'edge':>8}"
          f"{'t':>7}{'p~':>8}{'bootLo':>9}{'bootHi':>9}")
    for s in SIGNALS:
        if s == "BASELINE_always":
            continue
        for tier in ("all", "liquid", "illiquid"):
            r = tt[s]["_raw"][tier]
            b = base[tier]
            if len(r) < 2 or len(b) < 2:
                continue
            a = [X.net(t, 0.01, 0.01) for t in r]
            ma, mb = sum(a) / len(a), sum(b) / len(b)
            va = sum((x - ma) ** 2 for x in a) / (len(a) - 1)
            vb = sum((x - mb) ** 2 for x in b) / (len(b) - 1)
            se = math.sqrt(va / len(a) + vb / len(b))
            t = (ma - mb) / se if se else 0.0
            pv = math.erfc(abs(t) / math.sqrt(2))
            blo, bhi = _block_boot(r, tt["BASELINE_always"]["_raw"][tier])
            print(f"  {s:<17}{tier:<10}{len(a):>5}{pct(ma):>8}{pct(mb):>8}{pct(ma - mb):>8}"
                  f"{t:>7.2f}{pv:>8.3f}{pct(blo):>9}{pct(bhi):>9}")


def selftest(series, times, lo, hi, W):
    import random
    random.seed(7)
    cs = random.sample(list(series), min(12, len(series)))
    checked = 0
    for c in cs:
        p, v = series[c][3], series[c][4]
        for i in random.sample(range(lo, hi + 1), 12):
            full = features_at(p, v, i, W)
            trunc = features_at(p[:i + 1], v[:i + 1], i, W)
            assert full == trunc, f"LOOKAHEAD at {c} bar {i}"
            checked += 1
    print(f"selftest: {checked} (coin,hour) feature vectors bit-identical when the future is "
          f"physically deleted -- no lookahead in the hourly signal path.")


def ts(t):
    return datetime.fromtimestamp(t / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%MZ")


def run(par, times, series, uni_a, uni_b, args):
    W = WINDOWS[par]
    wu = warmup(W)
    lo, hi = wu, len(times) - 1 - FWD_MAX
    print("\n" + "=" * 118)
    print(f"PARAMETERISATION {par}: {W}")
    if hi <= lo:
        print(f"  UNUSABLE: warmup {wu}h + forward {FWD_MAX}h exceeds the {len(times)}h sample.")
        return None
    print(f"  warmup {wu}h, test bars {ts(times[lo])} .. {ts(times[hi])} ({hi - lo + 1} bars)")

    coins = [c for c in series if c != "bitcoin"]
    feats = {c: {i: features_at(series[c][3], series[c][4], i, W) for i in range(lo, hi + 1)}
             for c in coins}
    selftest(series, times, lo, hi, W)
    add_s0_fires(coins, feats, lo, hi, series["bitcoin"][3], W["s0_w"])
    fires = fire_map(coins, feats, lo, hi)

    res = {}
    for uname, uni in (("A_today_top300", uni_a), ("B_asof_start_top300", uni_b)):
        for t in TARGETS:
            r = evaluate(uni, feats, series, fires, lo, hi, t)
            res[(uname, t)] = r
            report_table(r, f"[{par}][{uname}] target {t} (peak +{TARGETS[t][0]:.0%} within "
                             f"{TARGETS[t][1]}h)  n_coins={len(uni)}")

    print("\n" + "-" * 118)
    print(f"[{par}] EXECUTION on real hourly OHLC (firings cooldown-deduped, {COOLDOWN}h)")
    for uname, uni in (("A_today_top300", uni_a), ("B_asof_start_top300", uni_b)):
        tier_at = tiers_by_bar(uni, series, lo, hi)
        tt = trades_for(uni, fires, series, tier_at)
        report_exec(tt, f"  [{uname}] liquid = top 50 by trailing-30d median 24h Binance "
                        f"quoteVolume (real exchange turnover)")
        edge_vs_baseline(tt, f"[{uname}] EDGE OVER THE UNCONDITIONAL BASELINE (net @1%/side)")
    resolution_check(uni_b, fires, series, times, lo, hi)
    return {"lo": lo, "hi": hi, "fires": fires, "res": res, "feats": feats}


def mina_report(par, st, times, series, uni_a, uni_b):
    print("\n" + "=" * 118)
    print(f"[{par}] MINA CHECK -- MINAUSDT, 15-25 Aug 2026")
    c = "mina-protocol"
    if c not in series:
        print("  no hourly data for mina-protocol")
        return
    o, h, l, cl, qv = series[c][:5]
    lo, hi = st["lo"], st["hi"]
    win = [i for i in range(len(times)) if "2026-08-15" <= ts(times[i])[:10] <= "2026-08-25"]
    for s in SIGNALS:
        idxs = [i for i in st["fires"][s].get(c, []) if i in set(win)]
        allf = st["fires"][s].get(c, [])
        if not idxs:
            print(f"  {s:<17} NO fire in 15-25 Aug  (total fires in sample: {len(allf)})")
            continue
        sh = ", ".join(f"{ts(times[i])}@${cl[i]:.4f}" for i in idxs[:10])
        print(f"  {s:<17} {len(idxs)} fires in 15-25 Aug: {sh}{' ...' if len(idxs) > 10 else ''}")
    # the feasibility study's specific claim
    tgt = [i for i in range(len(times)) if ts(times[i]) == "2026-08-19T17:00Z"]
    if tgt:
        i = tgt[0]
        f = vol10x(qv[:i + 1])
        print(f"\n  feasibility claim: >=10x trailing-median volume hour at 2026-08-19T17:00Z, $0.0421")
        print(f"    actual bar: o=${o[i]:.5f} h=${h[i]:.5f} l=${l[i]:.5f} c=${cl[i]:.5f} "
              f"quoteVol=${qv[i]:,.0f}")
        print(f"    vol / trailing-168h median = {f['mult']:.2f}x  -> rule "
              f"{'CONFIRMED' if vol10x_fires(f) else 'REFUTED'} at this bar")
        nb = [(j, vol10x(qv[:j + 1])) for j in win]
        peaks = sorted(((x["mult"], j) for j, x in nb if x), reverse=True)[:5]
        print("    top 5 volume-multiple hours in 15-25 Aug: "
              + ", ".join(f"{ts(times[j])}={m:.1f}x@${cl[j]:.5f}" for m, j in peaks))
    # how many OTHER coins the same rule fires on
    fs = st["fires"]["S5_vol10x"]
    tot = sum(len(v) for v in fs.values())
    print(f"\n  SAME RULE ACROSS THE WHOLE WINDOW: {tot} firing hours on "
          f"{len(fs)} of {len(series) - 1} coins "
          f"({len([c2 for c2 in uni_b if fs.get(c2)])} of {len(uni_b)} in the as-of universe).")
    print(f"  Median fires per firing coin: "
          f"{median([len(v) for v in fs.values()]) if fs else 0:.0f}. "
          f"A rule this common is a volume filter, not a stock-picker.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/truffle.db")
    ap.add_argument("--cache", default="data/cache")
    ap.add_argument("--asof", default=None, help="as-of date for universe B (default: sample start)")
    args = ap.parse_args()

    times, raw = load_bars(args.db)
    series = {c: v for c, v in raw.items()}
    print("=" * 118)
    print(f"HOURLY BACKTEST -- real Binance klines_1h")
    print(f"  {len(series)} coins, {len(times)} hourly bars, {ts(times[0])} .. {ts(times[-1])} "
          f"({len(times) / 24:.1f} days)")
    venue = defaultdict(int)
    for c, v in series.items():
        venue[v[6]] += 1
    print(f"  venue: {dict(venue)} (spot preferred; perp only where no spot pair exists)")

    asof = args.asof or ts(times[0])[:10]
    top300, pool = asof_universe(args.cache, asof)
    uni_a = [c for c in series if c != "bitcoin"]
    uni_b = [c for c in uni_a if c in top300]
    print(f"  universe A (today's top-300 mapped, survivorship-contaminated): {len(uni_a)} coins")
    print(f"  universe B (in the top 300 by market cap on {asof}, from the daily study's "
          f"{pool}-coin cached pool): {len(uni_b)} coins")
    print(f"    {len(uni_a) - len(uni_b)} coins are in A only because they grew into today's "
          f"top 300 after {asof}")

    regime(series, times, uni_b)
    states = {}
    for par in WINDOWS:
        st = run(par, times, series, uni_a, uni_b, args)
        if st:
            states[par] = st
            mina_report(par, st, times, series, uni_a, uni_b)
    print("\n" + "=" * 118)


if __name__ == "__main__":
    main()
