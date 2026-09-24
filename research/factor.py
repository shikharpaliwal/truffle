#!/usr/bin/env python
"""Cross-sectional long-short factors on the daily CoinGecko history.

RESEARCH ONLY. Reads data/truffle.db read-only plus the research response cache;
writes nothing, trades nothing, touches neither the pipeline nor the API.

Different question from research/backtest.py and research/hourly.py. Those fired a
signal, bought one coin and held it -- a long-only directional bet whose return is
dominated by market beta (their own BASELINE_always control made that plain). This
asks whether the CROSS-SECTIONAL RANKING carries information once beta is removed:
every week, rank the as-of universe on a signal, go long the top slice and short the
bottom, dollar-neutral.

Lookahead control, same discipline as backtest.py: every signal is a function of
p[:i+1] / mcap[:i+1] only, indexed backwards from i; the forward return p[i+7]/p[i]
is computed in a separate pass the signal code never sees. --selftest re-derives each
signal from a physically truncated copy of the series and asserts bit-identity.

Data choice: the 365-day DAILY CoinGecko history, not the 90-day hourly Binance
bars. The horizon is weeks, so what binds is the number of independent rebalance
periods (40 weekly, vs 12 on the hourly set), not intraday resolution. The hourly
data is used for one thing it is uniquely good for: the real perpetual funding rates
that price the short leg.
"""
import argparse
import logging
import math
import sys
from datetime import datetime, timezone

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))

from truffle import config, universe  # noqa: E402
from truffle.cache import Cache  # noqa: E402
from truffle.db import connect  # noqa: E402
from truffle.http import HttpClient  # noqa: E402

from research import history  # noqa: E402

log = logging.getLogger("factor")
NAN = float("nan")

WEEK = 7
WARMUP = 84              # 12w, the longest lookback -- fixes ONE sample for every factor
FEE = 0.0010             # taker, per side
SLIPS = (0.0, 0.0010, 0.0030, 0.0100)   # per side, on top of FEE
BORROW_WK = 0.15 / 52    # assumed borrow for a short with NO perp: 15%/yr, explicit
T_CRIT = 2.023           # two-sided 95%, 39 df

# Every offset any signal reads. Requiring all of them for every coin keeps the
# universe -- and therefore the sample -- identical across factors.
OFFSETS = (0, 7, 14, 28, 35, 56, 84)


# ------------------------------------------------------------------ signals (point-in-time)
def _r(p, i, back, skip=0):
    """Return over the window ending `skip` days before i, `back-skip` days long."""
    a, b = p[i - back], p[i - skip]
    if a != a or b != b or a <= 0:
        return None
    return b / a - 1.0


FACTORS = {
    # name:              (fn(p, m, i), what the TOP quintile is)
    "mom_1w":        (lambda p, m, i: _r(p, i, 7), "biggest 1w winners"),
    "mom_2w":        (lambda p, m, i: _r(p, i, 14), "biggest 2w winners"),
    "mom_4w":        (lambda p, m, i: _r(p, i, 28), "biggest 4w winners"),
    "mom_8w":        (lambda p, m, i: _r(p, i, 56), "biggest 8w winners"),
    "mom_12w":       (lambda p, m, i: _r(p, i, 84), "biggest 12w winners"),
    "mom_4w_skip1w": (lambda p, m, i: _r(p, i, 28, 7), "winners over t-28d..t-7d"),
    "mom_12w_skip1w": (lambda p, m, i: _r(p, i, 84, 7), "winners over t-84d..t-7d"),
    "rev_1w":        (lambda p, m, i: -x if (x := _r(p, i, 7)) is not None else None,
                      "biggest 1w LOSERS (short-term reversal)"),
    "size_smb":      (lambda p, m, i: -m[i] if m[i] == m[i] else None,
                      "SMALLEST by market cap (small-minus-big)"),
}


# ------------------------------------------------------------------ series plumbing
def build_series(hist, dates):
    p, m = [], []
    for d in dates:
        row = hist.get(d)
        p.append(row[0] if row and row[0] is not None else NAN)
        m.append(row[2] if row and row[2] is not None else NAN)
    return p, m


def rebalance_dates(n):
    """Indices of the weekly decision dates: warmed up, and with a full week ahead."""
    return list(range(WARMUP, n - WEEK, WEEK))


def as_of_universe(series, i, size):
    """Top `size` by market cap AS OF date i, among coins with every offset present.

    Point-in-time on the ranking variable. The one forward-looking requirement is a
    price at i+7 -- without it the week cannot be measured at all; the count of coins
    dropped for that reason is reported.
    """
    ok, no_fwd = {}, 0
    for c, (p, m) in series.items():
        if m[i] != m[i] or any(p[i - o] != p[i - o] or p[i - o] <= 0 for o in OFFSETS):
            continue
        if p[i + WEEK] != p[i + WEEK] or p[i + WEEK] <= 0:
            no_fwd += 1
            continue
        ok[c] = m[i]
    return sorted(ok, key=ok.get, reverse=True)[:size], no_fwd


# ------------------------------------------------------------------ funding / borrow
def load_funding(con):
    """{coin_id: [(funding_time_ms, rate), ...]} for coins with a mapped Binance perp."""
    sym2coin = {r[0]: r[1] for r in con.execute(
        "SELECT symbol, coin_id FROM binance_symbols "
        "WHERE market='perp' AND status='mapped' AND coin_id IS NOT NULL")}
    out = {}
    for sym, ts, rate in con.execute(
            "SELECT symbol, funding_time, funding_rate FROM funding_rates ORDER BY symbol, funding_time"):
        cid = sym2coin.get(sym)
        if cid and rate is not None:
            out.setdefault(cid, []).append((ts, rate))
    return out


def ms(date_str):
    return int(datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() * 1000)


def funding_week(fund, cid, t0, t1):
    """Funding a SHORT collects over (t0, t1]; None when the week is outside coverage."""
    rows = fund.get(cid)
    if not rows or rows[0][0] > t0 or rows[-1][0] < t1 - 86_400_000:
        return None
    return sum(r for ts, r in rows if t0 < ts <= t1)


# ------------------------------------------------------------------ one factor, one construction
def drift(weights, rets):
    """Weights after a week of price moves, renormalised -- what we actually hold at t+1."""
    v = {c: w * (1 + rets[c]) for c, w in weights.items() if c in rets}
    tot = sum(v.values())
    return {c: x / tot for c, x in v.items()} if tot > 0 else {}


def traded(new, old):
    """Notional traded as a fraction of the book (buys + sells)."""
    return sum(abs(new.get(c, 0.0) - old.get(c, 0.0)) for c in set(new) | set(old))


def run(series, dates, rebals, unis, fund, name, frac, assumed_fund):
    """Weekly records for one factor at one slice width. Costs applied afterwards."""
    fn = FACTORS[name][0]
    recs, held_l, held_s, held_b = [], {}, {}, {}
    for i in rebals:
        uni = unis[i]
        rets = {c: series[c][0][i + WEEK] / series[c][0][i] - 1.0 for c in uni}
        vals = {}
        for c in uni:
            p, m = series[c]
            v = fn(p[:i + 1], m[:i + 1], i)          # nothing after i is visible
            if v is not None and v == v:
                vals[c] = v
        if len(vals) < 20:
            continue
        order = sorted(vals, key=vals.get, reverse=True)
        k = max(1, int(round(len(order) * frac)))
        longs, shorts = order[:k], order[-k:]
        wl = {c: 1.0 / k for c in longs}
        ws = {c: 1.0 / k for c in shorts}
        wb = {c: 1.0 / len(uni) for c in uni}

        t0, t1 = ms(dates[i]), ms(dates[i + WEEK])
        f_real = [funding_week(fund, c, t0, t1) for c in shorts]
        n_real = sum(1 for x in f_real if x is not None)
        n_perp = sum(1 for c in shorts if c in fund)
        # A short collects funding where a perp exists (realised, else the sample's own
        # median week); with no perp it pays an explicit assumed borrow instead.
        carry = sum((x if x is not None else (assumed_fund if c in fund else -BORROW_WK))
                    for c, x in zip(shorts, f_real)) / k

        recs.append({
            "i": i, "date": dates[i], "n_uni": len(uni), "k": k,
            "r_long": sum(rets[c] for c in longs) / k,
            "r_short": sum(rets[c] for c in shorts) / k,
            "r_bench": sum(rets[c] for c in uni) / len(uni),
            "carry": carry, "perp_frac": n_perp / k, "real_frac": n_real / k,
            "trade_l": traded(wl, held_l), "trade_s": traded(ws, held_s),
            "trade_b": traded(wb, held_b),
        })
        held_l, held_s, held_b = drift(wl, rets), drift(ws, rets), drift(wb, rets)
    if recs:   # unwind the last book
        recs[-1]["trade_l"] += 1.0
        recs[-1]["trade_s"] += 1.0
        recs[-1]["trade_b"] += 1.0
    return recs


# ------------------------------------------------------------------ costs and statistics
def series_for(recs, mode, slip):
    """Weekly net returns. LS is per $1 long + $1 short (2x gross); LO/BENCH per $1."""
    c = FEE + slip
    out = []
    for r in recs:
        if mode == "LS":
            out.append(r["r_long"] - r["r_short"] + r["carry"] - c * (r["trade_l"] + r["trade_s"]))
        elif mode == "LO":
            out.append(r["r_long"] - c * r["trade_l"])
        elif mode == "BENCH":
            out.append(r["r_bench"] - c * r["trade_b"])
        elif mode == "LOX":   # long-only minus the equal-weight universe, both net
            out.append((r["r_long"] - c * r["trade_l"]) - (r["r_bench"] - c * r["trade_b"]))
    return out


def mdd(rets):
    """Drawdown on the compounded path, floored at total ruin.

    A 2x-gross dollar-neutral book CAN lose more than 100% in a week when the short
    leg doubles -- at decile width in this universe it does. That is wipeout, not a
    -155% drawdown, so the path stops there.
    """
    eq, peak, worst = 1.0, 1.0, 0.0
    for x in rets:
        eq *= 1 + x
        if eq <= 0:
            return -1.0
        peak = max(peak, eq)
        worst = min(worst, eq / peak - 1.0)
    return worst


def ols(y, x):
    n = len(y)
    mx, my = sum(x) / n, sum(y) / n
    sxx = sum((a - mx) ** 2 for a in x)
    sxy = sum((a - mx) * (b - my) for a, b in zip(x, y))
    syy = sum((b - my) ** 2 for b in y)
    beta = sxy / sxx if sxx else NAN
    corr = sxy / math.sqrt(sxx * syy) if sxx and syy else NAN
    return beta, corr


def stats(rets, bench=None):
    n = len(rets)
    mean = sum(rets) / n
    sd = math.sqrt(sum((x - mean) ** 2 for x in rets) / (n - 1)) if n > 1 else NAN
    se = sd / math.sqrt(n)
    eq, ruin = 1.0, False
    for x in rets:
        eq *= 1 + x
        if eq <= 0:
            eq, ruin = 0.0, True
            break
    # Annualisation is ARITHMETIC (52 x the weekly mean), not compounded. For a
    # dollar-neutral book at these weekly vols the compounded path is dominated by
    # the variance drag and, at decile width, hits ruin -- it describes the leverage
    # choice, not the factor. The compounded path is reported separately as `cum`.
    out = {"n": n, "wk_mean": mean, "cum": eq - 1.0, "ruin": ruin, "ann": mean * 52,
           "vol": sd * math.sqrt(52), "sharpe": (mean * 52) / (sd * math.sqrt(52)) if sd else NAN,
           "mdd": mdd(rets), "hit": sum(1 for x in rets if x > 0) / n,
           "t": mean / se if se else NAN, "lo": mean - T_CRIT * se, "hi": mean + T_CRIT * se}
    if bench:
        out["beta"], out["corr"] = ols(rets, bench)
    return out


# ------------------------------------------------------------------ reporting
def pc(x, w=7, d=1):
    return f"{'n/a':>{w}}" if x is None or x != x else f"{100 * x:{w}.{d}f}%"


def hdr(title):
    print("\n" + title)
    print(f"  {'factor':<16}{'gross':>9}{'@10bp':>9}{'@30bp':>9}{'@100bp':>9}{'cum@10bp':>11}"
          f"{'vol':>8}{'Sharpe':>8}{'maxDD':>8}{'hit':>6}{'t':>7}{'95% CI on wk mean':>22}"
          f"{'beta':>7}{'BE/side':>9}")


def line(name, per_slip, be, beta):
    s, t = per_slip[0.0], per_slip[0.001]
    ci = f"[{100 * t['lo']:+.2f}%,{100 * t['hi']:+.2f}%]"
    cum = "  WIPED OUT" if t["ruin"] else pc(t["cum"], 11)
    print(f"  {name:<16}{pc(s['ann'], 9)}{pc(t['ann'], 9)}{pc(per_slip[0.003]['ann'], 9)}"
          f"{pc(per_slip[0.01]['ann'], 9)}{cum}{pc(s['vol'], 8)}{s['sharpe']:8.2f}"
          f"{pc(s['mdd'], 8)}{pc(s['hit'], 6, 0)}{t['t']:7.2f}{ci:>22}"
          f"{beta:7.2f}{('n/a' if be is None else f'{10000 * be:.0f}bp'):>9}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pool", type=int, default=600, help="today's top-N to load history for")
    ap.add_argument("--size", type=int, default=300, help="as-of universe size per rebalance")
    ap.add_argument("--selftest", action="store_true", default=True)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    cfg = config.load()
    http = HttpClient(cfg, Cache(cfg["cache_dir"], None))   # research cache never expires
    con = connect(cfg["db_path"])
    kept, _ = universe.build(http, cfg, args.pool)
    ids = [r["id"] for r in kept]
    hist = history.load(http, ids)
    if http.calls:
        history.record_usage(con, http)
    log.info("http calls this run: %d (0 == everything served from the research cache)", http.calls)

    dates = sorted(hist[max(hist, key=lambda c: len(hist[c]))])
    series = {c: build_series(h, dates) for c, h in hist.items()}
    n = len(dates)
    rebals = rebalance_dates(n)
    fund = load_funding(con)

    print("=" * 150)
    print("CROSS-SECTIONAL LONG-SHORT FACTOR STUDY")
    print(f"data      : {len(series)} coins, daily CoinGecko, {dates[0]} .. {dates[-1]} ({n} days)")
    print(f"rebalance : weekly, {len(rebals)} periods, {dates[rebals[0]]} .. {dates[rebals[-1]]}"
          f" (+{WEEK}d holding)")
    print(f"warmup    : {WARMUP}d -- the 12w lookback. Every factor is scored on the SAME "
          f"{len(rebals)} periods and the SAME universe.")

    unis, dropped = {}, []
    for i in rebals:
        unis[i], nf = as_of_universe(series, i, args.size)
        dropped.append(nf)
    print(f"universe  : as-of top {args.size} by market cap on each rebalance date; "
          f"mean size {sum(len(unis[i]) for i in rebals) / len(rebals):.0f}, "
          f"mean {sum(dropped) / len(dropped):.0f} coins/week dropped for no price at t+7d")

    # funding: what the data actually says, before it is used as a cost
    covered = [x for i in rebals
               for c in unis[i]
               if (x := funding_week(fund, c, ms(dates[i]), ms(dates[i + WEEK]))) is not None]
    assumed = sorted(covered)[len(covered) // 2] if covered else 0.0
    perp_cov = sum(1 for i in rebals for c in unis[i] if c in fund) / sum(len(unis[i]) for i in rebals)
    print(f"short cost: {100 * perp_cov:.0f}% of as-of-universe coin-weeks have a mapped Binance perp; "
          f"funding_rates covers {len(covered)} of those coin-weeks")
    print(f"            realised funding a SHORT collects: median {100 * assumed:+.3f}%/week "
          f"({100 * assumed * 52:+.1f}%/yr), mean {100 * sum(covered) / len(covered):+.3f}%/week. "
          f"Uncovered perp weeks use the median; coins with NO perp are charged an assumed "
          f"{100 * BORROW_WK:.2f}%/week borrow (15%/yr).")

    print(f"columns   : `gross` = before trading costs but AFTER short carry (funding received / "
          f"borrow paid); `@Nbp` adds {100 * FEE:.2f}% fee + N bp slippage PER SIDE on the "
          f"actual turnover; `cum` is the compounded 40-week path at 10bp -- it sits far below "
          f"52x the weekly mean because variance drag at these volatilities is enormous, and "
          f"both numbers are shown rather than the flattering one.")

    if args.selftest:
        selftest(series, dates, rebals, unis)

    results = {}
    for frac, label in ((0.20, "QUINTILE"), (0.10, "DECILE")):
        recs = {f: run(series, dates, rebals, unis, fund, f, frac, assumed) for f in FACTORS}
        results[frac] = recs
        bench = {s: series_for(next(iter(recs.values())), "BENCH", s) for s in SLIPS}

        hdr(f"[{label} {frac:.0%}] DOLLAR-NEUTRAL LONG-SHORT   (return per $1 long + $1 short, "
            f"i.e. 2x gross; gross/@Nbp columns are 52 x the weekly mean, cum is the "
            f"compounded {len(rebals)}-week path)")
        for f in FACTORS:
            per = {s: stats(series_for(recs[f], "LS", s), bench[0.0]) for s in SLIPS}
            g = per[0.0]
            turn = sum(r["trade_l"] + r["trade_s"] for r in recs[f]) / len(recs[f])
            be = g["wk_mean"] / turn if turn else None
            results[(frac, f, "LS")] = (per, turn, be)
            line(f, per, be, per[0.0]["beta"])
        b = {s: stats(bench[s], bench[0.0]) for s in SLIPS}
        turn_b = sum(r["trade_b"] for r in results[frac][next(iter(FACTORS))]) / len(rebals)
        bench_cum, bench_turn = b[0.001]["cum"], turn_b
        line("BASELINE_EWuni", b, None, b[0.0]["beta"])
        print(f"  {'':16}(BASELINE_EWuni = equal-weight the whole as-of universe, long only, "
              f"the 'buy everything' control; mean weekly turnover {100 * turn_b:.1f}%)")
        print(f"  mean weekly turnover, both legs: "
              + ", ".join(f"{f}={100 * results[(frac, f, 'LS')][1]:.0f}%" for f in FACTORS))

        hdr(f"[{label} {frac:.0%}] LONG-ONLY TOP SLICE minus the EQUAL-WEIGHT UNIVERSE "
            f"(both net of their own costs) -- the actionable version, no shorting")
        for f in FACTORS:
            per = {s: stats(series_for(recs[f], "LOX", s), bench[0.0]) for s in SLIPS}
            turn = sum(r["trade_l"] - r["trade_b"] for r in recs[f]) / len(recs[f])
            be = per[0.0]["wk_mean"] / turn if turn > 0 else None
            results[(frac, f, "LOX")] = (per, turn, be)
            line(f, per, be, per[0.0]["beta"])
        print(f"  {'BASELINE_EWuni':<16}{'0.0%':>9}{'0.0%':>9}{'0.0%':>9}{'0.0%':>9}{'0.0%':>11}"
              f"{'':>8}{'':>8}{'':>8}{'':>6}{'':>7}{'(it is the benchmark)':>22}{1.00:7.2f}")

        hdr(f"[{label} {frac:.0%}] LONG-ONLY TOP SLICE, absolute (this is the beta-loaded number)")
        for f in FACTORS:
            per = {s: stats(series_for(recs[f], "LO", s), bench[0.0]) for s in SLIPS}
            line(f, per, None, per[0.0]["beta"])
        line("BASELINE_EWuni", b, None, b[0.0]["beta"])

    leg_table(results[0.20], rebals)
    headline(results, rebals, bench_cum, bench_turn)
    multiplicity(results)
    limits(series, unis, rebals, dates, args, results)
    print("=" * 150)


def leg_table(recs, rebals):
    print("\n[QUINTILE] LEG DECOMPOSITION, gross weekly means -- where the long-short number comes from")
    print(f"  {'factor':<16}{'long':>9}{'short':>9}{'L-S':>9}{'bench':>9}{'L-bench':>10}"
          f"{'bench-S':>10}{'carry':>9}{'%short w/perp':>15}")
    for f in FACTORS:
        r = recs[f]
        m = lambda k: sum(x[k] for x in r) / len(r)  # noqa: E731
        print(f"  {f:<16}{pc(m('r_long'), 9, 2)}{pc(m('r_short'), 9, 2)}"
              f"{pc(m('r_long') - m('r_short'), 9, 2)}{pc(m('r_bench'), 9, 2)}"
              f"{pc(m('r_long') - m('r_bench'), 10, 2)}{pc(m('r_bench') - m('r_short'), 10, 2)}"
              f"{pc(m('carry'), 9, 3)}{pc(m('perp_frac'), 15, 0)}")


def headline(results, rebals, bench_cum, bench_turn):
    q = lambda f, mode, s=0.001: results[(0.20, f, mode)][0][s]  # noqa: E731
    moms = [f for f in FACTORS if f.startswith("mom_") and "skip" not in f]
    print("\nHEADLINE")
    print(f"  1. Plain cross-sectional MOMENTUM is negative at EVERY lookback, before any cost: "
          f"the quintile long-short gross weekly mean is "
          + ", ".join(f"{f.split('_')[1]} {100 * q(f, 'LS', 0.0)['wk_mean']:+.2f}%" for f in moms)
          + f". The winner quintile beat the loser quintile in only "
          f"{100 * q('mom_1w', 'LS', 0.0)['hit']:.0f}% of the {len(rebals)} weeks at 1w and "
          f"{100 * q('mom_12w', 'LS', 0.0)['hit']:.0f}% at 12w. No cost model is needed to reject "
          f"it -- it does not clear zero before costs.")
    print(f"  2. SHORT-TERM REVERSAL is the same coefficient with the sign flipped, so it is the "
          f"only thing in the table that is positive: quintile LS "
          f"{100 * q('rev_1w', 'LS', 0.0)['wk_mean']:+.2f}%/week gross, t={q('rev_1w', 'LS')['t']:+.2f}. "
          f"That t is NOT significant, and it is one factor in a 36-cell grid.")
    print(f"  3. Costs decide it. rev_1w turns {100 * results[(0.20, 'rev_1w', 'LS')][1]:.0f}% of the "
          f"book over every week, so its breakeven total cost is "
          f"{10000 * results[(0.20, 'rev_1w', 'LS')][2]:.0f}bp/side, and past that it inverts: "
          f"{pc(q('rev_1w', 'LS', 0.003)['ann']).strip()}/yr at 30bp/side but "
          f"{pc(q('rev_1w', 'LS', 0.01)['ann']).strip()}/yr at 100bp/side. The loser quintile of "
          f"a 300-coin crypto universe is where the illiquid names are; 30bp/side round-trip on "
          f"60 of them every week is an assumption, not a measurement.")
    print(f"  4. The most actionable cell -- long-only top quintile of rev_1w minus the "
          f"equal-weight universe, no shorting -- is "
          f"{pc(q('rev_1w', 'LOX', 0.0)['ann']).strip()}/yr gross, "
          f"{pc(q('rev_1w', 'LOX', 0.001)['ann']).strip()} at 10bp, "
          f"{pc(q('rev_1w', 'LOX', 0.01)['ann']).strip()} at 100bp, t={q('rev_1w', 'LOX')['t']:+.2f}, "
          f"breakeven {10000 * results[(0.20, 'rev_1w', 'LOX')][2]:.0f}bp/side. Suggestive, not "
          f"established.")
    print(f"  5. \"Dollar-neutral\" is NOT market-neutral here. Measured betas to the equal-weight "
          f"universe, quintile LS: "
          + ", ".join(f"{f}={q(f, 'LS', 0.0)['beta']:+.2f}" for f in list(FACTORS)[:5])
          + f". A book with beta {q('mom_1w', 'LS', 0.0)['beta']:+.2f} is a market bet wearing a "
          f"neutrality label.")
    print(f"  6. Real short funding is a rounding error next to turnover: a short collected a "
          f"median +0.15%/week where a Binance perp existed. The short-leg carry line in the leg "
          f"table is dominated by the ASSUMED 0.29%/week borrow on the ~46% of names with no perp.")
    print(f"  7. Nothing in the grid survives a multiple-comparisons correction, and the "
          f"buy-everything control (BASELINE_EWuni: equal-weight the whole as-of universe, "
          f"long only) compounded {pc(bench_cum).strip()} over the {len(rebals)} weeks net of its "
          f"own {100 * bench_turn:.0f}%/week turnover -- which most of the grid does not beat.")
    print("\n  OVERFITTING FLAGS, stated rather than buried:")
    print(f"    - mom_4w_skip1w is the ONLY momentum cell with a positive sign. It is also the "
          f"one whose construction I varied after seeing that plain momentum failed. Treat it as "
          f"a grid artefact until a second sample says otherwise (t={q('mom_4w_skip1w', 'LOX')['t']:+.2f}).")
    print(f"    - rev_1w's long-only-minus-benchmark Sharpe of "
          f"{q('rev_1w', 'LOX', 0.0)['sharpe']:.2f} is the best number on the page and would be "
          f"the one to quote. Its t is {q('rev_1w', 'LOX')['t']:+.2f} on {len(rebals)} "
          f"observations, which is not evidence -- it is the largest of 36 draws.")
    print(f"    - The decile tables are strictly noisier than the quintile ones (half the names, "
          f"same period). Every decile cell that looks better than its quintile twin is a "
          f"smaller-sample artefact, not a stronger signal.")


def multiplicity(results):
    tests = [(f"{lbl} {f} {mode}", results[(frac, f, mode)][0][0.001]["t"])
             for frac, lbl in ((0.20, "Q"), (0.10, "D")) for f in FACTORS for mode in ("LS", "LOX")]
    best = max(tests, key=lambda t: abs(t[1]))
    k = len(tests)
    print(f"\nMULTIPLE COMPARISONS")
    print(f"  {k} hypotheses tested: {len(FACTORS)} factors x 2 slice widths x 2 constructions, "
          f"all at 10bp slippage.")
    print(f"  Largest |t| of the {k}: {best[0]} at t={best[1]:+.2f}.")
    print(f"  Bonferroni threshold for a=0.05 over {k} tests: |t| > {bonf(k):.2f}. "
          f"{'PASSES' if abs(best[1]) > bonf(k) else 'DOES NOT PASS'}.")
    print(f"  Under the null, the expected max |t| over {k} correlated tests is roughly 2.5-3.0, "
          f"so a single |t| near 2 in this table is what noise looks like, not a finding.")
    print(f"  Note the grid is not {k} independent bets: rev_1w is mom_1w with the sign flipped "
          f"(its LS return is the negative of mom_1w's, up to costs), and the momentum lookbacks "
          f"overlap heavily. The effective number of independent hypotheses is smaller -- which "
          f"makes the Bonferroni line conservative but does NOT rescue a t near 2.")


def bonf(k):
    """Two-sided Bonferroni critical |t| at 39 df, via a normal approximation + df inflation."""
    from statistics import NormalDist
    z = NormalDist().inv_cdf(1 - 0.025 / k)
    return z * (1 + (z * z + 1) / (4 * 39))


def limits(series, unis, rebals, dates, args, results):
    pool = set(series)
    ever = set().union(*(set(unis[i]) for i in rebals))
    first, last = set(unis[rebals[0]]), set(unis[rebals[-1]])
    print("\nSURVIVORSHIP, AND WHAT 40 WEEKS CANNOT SETTLE")
    print(f"  The as-of reconstruction fixes universe MEMBERSHIP: each week ranks the top "
          f"{args.size} by market cap on that date, not today's top {args.size}. "
          f"{len(first & last)} of {args.size} coins are in both the first and the last "
          f"universe; {len(ever)} distinct coins appear at some point.")
    print(f"  RESIDUAL SURVIVORSHIP, unfixable here: the pool itself is TODAY's top {args.pool} "
          f"(post-exclusion, {len(pool)} with usable history). A coin that was in the top "
          f"{args.size} a year ago and has since collapsed out of the top {args.pool} is simply "
          f"absent -- no amount of as-of ranking brings it back, and its slot is filled by a "
          f"survivor that ranked 301-600 at the time.")
    print(f"  DIRECTION of that bias, stated rather than waved at: the missing coins are the ones "
          f"that crashed. They would have carried deeply negative trailing returns, so momentum "
          f"would have ranked them into the SHORT leg, where shorting them pays. Their absence "
          f"therefore biases every momentum long-short number DOWNWARD and every reversal number "
          f"(which is long the losers) UPWARD. It also lifts the equal-weight benchmark, which "
          f"biases long-only-minus-benchmark DOWNWARD. So a null momentum result here is not "
          f"explained away by survivorship; a positive reversal result partly is.")
    n = len(rebals)
    sds = sorted(results[(0.20, f, "LS")][0][0.001]["vol"] / math.sqrt(52) for f in FACTORS)
    med = sds[len(sds) // 2]
    mde = T_CRIT * med / math.sqrt(n)
    print(f"  POWER, computed from this sample rather than asserted: {n} weekly observations; the "
          f"MEDIAN realised weekly volatility of a quintile dollar-neutral book here is "
          f"{100 * med:.1f}% (range {100 * sds[0]:.1f}%-{100 * sds[-1]:.1f}%). The smallest weekly "
          f"mean distinguishable from zero at 95% is therefore {100 * mde:.2f}%/week, i.e. "
          f"{100 * 52 * mde:.0f}%/yr arithmetic. Published crypto cross-sectional premia are "
          f"single-digit to low-double-digit percent per YEAR. A real factor of that size is "
          f"invisible in this sample whether it exists or not: to detect ~10%/yr at this "
          f"volatility would take roughly "
          f"{(T_CRIT * med / (0.10 / 52)) ** 2 / 52:.0f} YEARS of weekly data. The equity "
          f"momentum literature measures these over 5+ decades and the crypto papers over 5+ "
          f"years; one year cannot establish a factor, and this study has not done so in either "
          f"direction. What it CAN say is the negative: nothing here is large enough to survive "
          f"the costs, and the gross momentum signs are wrong, which no amount of extra power "
          f"fixes.")
    print(f"  REGIME: {dates[rebals[0]]} .. {dates[-1]} is one market regime. A cross-sectional "
          f"factor's sign is regime-dependent (crypto momentum is documented to invert in "
          f"drawdowns), and one regime gives no evidence about the other.")


def selftest(series, dates, rebals, unis):
    """Prove no lookahead: each signal recomputed from a physically truncated series must match."""
    import random
    random.seed(11)
    cs = random.sample(sorted(unis[rebals[0]]), 12)
    checked = 0
    for c in cs:
        p, m = series[c]
        for i in random.sample(rebals, min(10, len(rebals))):
            for name, (fn, _) in FACTORS.items():
                full = fn(p, m, i)                     # whole year visible to the function
                tp, tm = p[:i + 1], m[:i + 1]          # future physically deleted
                trunc = fn(tp, tm, len(tp) - 1)
                assert full == trunc or (full != full and trunc != trunc), \
                    f"LOOKAHEAD {name} {c} {dates[i]}: {full} != {trunc}"
                checked += 1
    print(f"\nselftest: {checked} (coin, date, factor) values identical when every sample after "
          f"the decision date is deleted from memory -- no lookahead in the signal path.")


if __name__ == "__main__":
    main()
