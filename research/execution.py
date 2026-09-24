"""Turn a firing into a simulated trade with costs, a real exit, and gap risk.

DATA LIMIT, stated up front: CoinGecko market_chart gives ONE price snapshot per UTC
day -- no open/high/low. So "next bar's open" is the next daily snapshot, a stop can
only be detected on a daily observation, and intraday spikes through the stop are
invisible. That biases stop-outs DOWNWARD (optimistic) while the gap-through
modelling below is realistic: an exit fills at the observed price, never at the stop.
"""
from statistics import median

FEE = 0.0010          # taker, per side -> 20bps round trip
NOTIONAL = 10_000.0   # assumed position size, USD
STOP, TP, HOLD = 0.20, 0.40, 14
SPREAD_FLOOR, IMPACT_C = 0.0015, 0.10   # slip = 15bps + 10% * sqrt(participation)
FIXED_SLIPS = (0.003, 0.01, 0.03)
ILLIQ_PARTICIPATION = 0.01              # >1% of a day's dollar volume == not exitable


def adv(v, i, look=30):
    """Trailing median daily dollar volume ending at i (point-in-time)."""
    w = [x for x in v[max(0, i - look + 1):i + 1] if x == x]
    return median(w) if w else None


def vol_slip(a):
    if not a or a <= 0:
        return 0.05
    return SPREAD_FLOOR + IMPACT_C * (NOTIONAL / a) ** 0.5


def simulate(p, v, t):
    """One trade from a firing at index t. Path is cost-independent; costs applied after.

    Entry: the NEXT daily snapshot (t+1) -- never the signal bar.
    Exit, first to happen within HOLD days: stop at -20% (filled at the observed
    price, i.e. gapped, not at the stop level), take-profit at +40% (filled at the
    limit, since price traded through it), else time-exit.
    """
    e = t + 1
    n = len(p)
    if e + 1 >= n or p[e] != p[e] or p[e] <= 0:
        return None
    ref = p[e]
    stop_lv, tp_lv = ref * (1 - STOP), ref * (1 + TP)
    last = min(n - 1, e + HOLD)
    px, why, xi, gap = p[last], "time", last, 0.0
    lo = ref
    for d in range(e + 1, last + 1):
        if p[d] != p[d]:
            continue
        lo = min(lo, p[d])
        if p[d] <= stop_lv:
            px, why, xi = p[d], "stop", d
            gap = stop_lv / p[d] - 1.0        # how far below the stop we actually filled
            break
        if p[d] >= tp_lv:
            px, why, xi = tp_lv, "tp", d
            break
    if px != px:
        return None
    a_in, a_out = adv(v, e), adv(v, xi)
    return {"entry_i": e, "exit_i": xi, "why": why, "gross": px / ref - 1.0,
            "gap": gap, "mdd": lo / ref - 1.0, "days": xi - e,
            "adv_in": a_in, "adv_out": a_out,
            "part": (NOTIONAL / a_in) if a_in else None,
            "slip_in": vol_slip(a_in), "slip_out": vol_slip(a_out)}


def net(tr, s_in, s_out):
    return (1 + tr["gross"]) * (1 - s_out) / (1 + s_in) - 1 - 2 * FEE


def breakeven_slip(trades):
    """Per-side slippage at which mean expectancy hits zero (negative => already dead)."""
    if not trades:
        return None
    g = sum(1 + t["gross"] for t in trades) / len(trades)
    k = 1 + 2 * FEE
    return (g - k) / (g + k)


def summarise(trades):
    if not trades:
        return None
    n = len(trades)
    out = {"n": n, "median_days": median(t["days"] for t in trades),
           "pct_stop": sum(1 for t in trades if t["why"] == "stop") / n,
           "pct_tp": sum(1 for t in trades if t["why"] == "tp") / n,
           "pct_gap_2pct": sum(1 for t in trades if t["gap"] > 0.02) / n,
           "median_gap_on_stops": median([t["gap"] for t in trades if t["why"] == "stop"] or [0]),
           "median_mdd": median(t["mdd"] for t in trades),
           "worst_mdd": min(t["mdd"] for t in trades),
           "gross_mean": sum(t["gross"] for t in trades) / n,
           "breakeven_slip": breakeven_slip(trades)}
    parts = [t["part"] for t in trades if t["part"]]
    out["median_part"] = median(parts) if parts else None
    out["pct_not_exitable"] = (sum(1 for x in parts if x > ILLIQ_PARTICIPATION) / len(parts)
                               if parts else None)
    for s in FIXED_SLIPS:
        r = sorted(net(t, s, s) for t in trades)
        out[f"exp_{s}"] = sum(r) / n
        out[f"med_{s}"] = median(r)
        out[f"p10_{s}"] = r[max(0, int(0.10 * n) - 1)]
        out[f"win_{s}"] = sum(1 for x in r if x > 0) / n
    r = sorted(net(t, t["slip_in"], t["slip_out"]) for t in trades)
    out["exp_vol"] = sum(r) / n
    out["med_vol"] = median(r)
    out["p10_vol"] = r[max(0, int(0.10 * n) - 1)]
    out["win_vol"] = sum(1 for x in r if x > 0) / n
    return out
