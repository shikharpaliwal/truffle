"""Point-in-time signals.

EVERY function here takes sequences that have ALREADY been truncated at the decision
date t -- index -1 IS date t and nothing later exists in the argument. Only negative
indexing is used, so no function can reach forward even if handed a longer array.
Labels/forward returns live in backtest.py and never touch these.
"""
import math
from statistics import median, pstdev

NAN = float("nan")


def _ok(x):
    return x is not None and x == x


def _win(a, n, off=0):
    """The n samples ending `off` days before t, or None if unavailable/incomplete."""
    hi = len(a) - off
    lo = hi - n
    if lo < 0:
        return None
    s = a[lo:hi]
    return s if all(_ok(x) for x in s) else None


def _at(a, off):
    """Value `off` days before t."""
    i = len(a) - 1 - off
    return a[i] if i >= 0 and _ok(a[i]) else None


def _mean(s):
    return sum(s) / len(s)


def _ret(p, k):
    """Return over the k days ending at t."""
    a, b = _at(p, k), _at(p, 0)
    return None if not a or b is None else b / a - 1.0


# ---------------------------------------------------------------- S0: production logic
def s0_raw(p, v, w=30):
    """The two price/volume inputs truffle/scoring.py momentum() consumes, at date t.

    vol_mom: log-ratio of the trailing w-day volume sum to the prior w-day sum.
    ret_cur/ret_prior: w-day returns, turned into rel_btc by the caller.
    """
    cur_v, pri_v = _win(v, w), _win(v, w, w)
    p_now, p_mid, p_old = _at(p, 0), _at(p, w), _at(p, 2 * w)
    if cur_v is None or pri_v is None or not p_mid or not p_old or p_now is None:
        return None
    return {"vol_mom": math.log((sum(cur_v) + 1.0) / (sum(pri_v) + 1.0)),
            "ret_cur": p_now / p_mid - 1.0, "ret_prior": p_mid / p_old - 1.0}


# ---------------------------------------------------------------- S1: 7d vs 30d
S1_PRICE, S1_VOL = 1.10, 1.5


def s1(p, v):
    pw, pl, vw, vl = _win(p, 7), _win(p, 30), _win(v, 7), _win(v, 30)
    if pw is None or pl is None or vw is None or vl is None or _mean(pl) <= 0 or _mean(vl) <= 0:
        return None
    return {"price_ratio": _mean(pw) / _mean(pl), "vol_ratio": _mean(vw) / _mean(vl)}


def s1_fires(f):
    return bool(f) and f["price_ratio"] >= S1_PRICE and f["vol_ratio"] >= S1_VOL


# ---------------------------------------------------------------- S2: volume surge z
S2_Z = 3.0


def s2(p, v, recent=3, base=90, gap=7):
    """z of the last `recent` days' mean volume against the 90d window ending `gap` days back.

    The gap keeps the surge itself out of its own baseline, which would otherwise
    inflate the mean and sd and mute the very spike we are looking for.
    """
    r, b = _win(v, recent), _win(v, base, gap)
    if r is None or b is None:
        return None
    sd = pstdev(b)
    if sd <= 0:
        return None
    return {"z": (_mean(r) - _mean(b)) / sd}


def s2_fires(f):
    return bool(f) and f["z"] >= S2_Z


# ---------------------------------------------------------------- S3: acceleration
S3_ACCEL, S3_MIN_RET = 0.15, 0.05


def s3(p, v, k=7):
    """This k-day return minus the previous k-day return."""
    now, mid, old = _at(p, 0), _at(p, k), _at(p, 2 * k)
    if now is None or not mid or not old:
        return None
    r1, r0 = now / mid - 1.0, mid / old - 1.0
    return {"accel": r1 - r0, "ret": r1}


def s3_fires(f):
    return bool(f) and f["accel"] >= S3_ACCEL and f["ret"] >= S3_MIN_RET


# ---------------------------------------------------------------- S4: + extension filter
S4_EXT = 1.25   # reject if price is already >25% above its 60d median


def extension(p, look=60):
    w = _win(p, look)
    m = median(w) if w else None
    now = _at(p, 0)
    return None if not m or now is None else {"ext": now / m}


def not_extended(e):
    return bool(e) and e["ext"] <= S4_EXT
