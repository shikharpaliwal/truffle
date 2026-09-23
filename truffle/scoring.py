import math

# rel_btc is already a return (can be negative), so its momentum is a plain
# difference; every other metric is a non-negative magnitude where a log-ratio
# keeps 2x-up and 2x-down symmetric and stops whales dominating the ranking.
DIFF_METRICS = {"rel_btc"}
EPS = 1.0


def momentum(key, current, prior):
    if current is None or prior is None:
        return None
    if key in DIFF_METRICS:
        return current - prior
    if current < 0 or prior < 0:
        return None
    return math.log((current + EPS) / (prior + EPS))


def change_pct(current, prior):
    if current is None or prior is None or prior == 0:
        return None
    return (current - prior) / abs(prior) * 100.0


def rank_scores(values):
    """Percentile-rank a {key: momentum} map to 0-100. Nulls stay null. Ties share the mean rank."""
    present = {k: v for k, v in values.items() if v is not None and not math.isnan(v)}
    out = {k: None for k in values}
    n = len(present)
    if n == 0:
        return out
    if n == 1:
        return {**out, next(iter(present)): 50.0}
    ordered = sorted(present.items(), key=lambda kv: kv[1])
    i = 0
    while i < len(ordered):
        j = i
        while j + 1 < len(ordered) and ordered[j + 1][1] == ordered[i][1]:
            j += 1
        mean_idx = (i + j) / 2
        score = round(100.0 * mean_idx / (n - 1), 4)
        for k, _ in ordered[i:j + 1]:
            out[k] = score
        i = j + 1
    return out


NEUTRAL = 50.0  # the midpoint of a percentile scale: "average coin"


def spread_factor(present_keys, weights):
    """How much narrower a full-coverage composite is than one built from `present_keys`.

    A weighted mean of roughly independent percentiles has spread proportional to
    sqrt(sum p_i^2) over the renormalised weights, so averaging FEWER metrics widens the
    distribution and sparse coins monopolise both extremes. Rescaling the centred score by
    this factor (<= 1, exactly 1 at full coverage) equalises spread across coverage levels.
    It depends only on WHICH metrics are present, never on their values, so it cannot
    reorder two coins that have the same present-set.
    """
    total = sum(weights[k] for k in present_keys)
    full = sum(w * w for w in weights.values()) / sum(weights.values()) ** 2
    part = sum(weights[k] ** 2 for k in present_keys) / total ** 2
    return math.sqrt(full / part)


def weighted_score(metric_ranks, weights, shrink=True):
    """Average the available rank_scores, renormalising weights. Nulls are dropped, never zeroed.

    Returns (score, score_raw, missing, coverage). `score` is `score_raw` pulled toward
    NEUTRAL by spread_factor unless `shrink` is off, in which case the two are equal.
    """
    present = {k: v for k, v in metric_ranks.items() if v is not None and weights.get(k)}
    missing = sorted(k for k in weights if k not in present)
    total = sum(weights[k] for k in present)
    if not total:
        return None, None, missing, 0.0
    raw = sum(weights[k] * v for k, v in present.items()) / total
    f = spread_factor(present, weights) if shrink else 1.0
    score = NEUTRAL + (raw - NEUTRAL) * f
    return round(score, 4), round(raw, 4), missing, round(total / sum(weights.values()), 4)


def score_change(score, score_prev):
    if score is None or score_prev is None:
        return None
    return round(score - score_prev, 4)
