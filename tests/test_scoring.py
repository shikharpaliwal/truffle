import pytest

from truffle.scoring import (change_pct, momentum, rank_scores, score_change, spread_factor,
                             weighted_score)

# The production composite (config.yaml `weights`).
W = {"volume_reputable": 0.20, "rel_btc": 0.15}

# scoring.py takes its weight map as an argument, so the shrinkage maths is exercised over
# a synthetic six-metric map: two weighted metrics only give two coverage levels, which
# cannot show that f is monotone in coverage or that it never re-ranks within a bucket.
WIDE = {"a": 0.2, "b": 0.2, "c": 0.2, "d": 0.15, "e": 0.1, "f": 0.15}


def test_rank_transform_spans_full_range():
    out = rank_scores({"a": 1.0, "b": 2.0, "c": 3.0, "d": 4.0})
    assert out["a"] == 0.0 and out["d"] == 100.0
    assert out["b"] < out["c"]


def test_rank_transform_preserves_order_not_magnitude():
    out = rank_scores({"a": 1.0, "b": 2.0, "c": 1000.0})
    assert [out["a"], out["b"], out["c"]] == [0.0, 50.0, 100.0]


def test_rank_transform_nulls_stay_null_and_do_not_shift_ranks():
    out = rank_scores({"a": 1.0, "b": 2.0, "c": None})
    assert out["c"] is None
    assert out["a"] == 0.0 and out["b"] == 100.0


def test_rank_transform_ties_share_mean_rank():
    out = rank_scores({"a": 5.0, "b": 5.0, "c": 9.0})
    assert out["a"] == out["b"] == 25.0 and out["c"] == 100.0


def test_rank_transform_degenerate_cases():
    assert rank_scores({}) == {}
    assert rank_scores({"a": None}) == {"a": None}
    assert rank_scores({"a": 3.0}) == {"a": 50.0}


def test_momentum_log_ratio_is_symmetric():
    up, down = momentum("tvl", 200, 100), momentum("tvl", 100, 200)
    assert up == pytest.approx(-down, abs=0.02)


def test_momentum_handles_zero_prior():
    assert momentum("tvl", 10, 0) > 0
    assert momentum("tvl", 0, 0) == 0.0


def test_momentum_rel_btc_is_a_difference():
    assert momentum("rel_btc", 0.1, -0.2) == pytest.approx(0.3)


def test_momentum_requires_both_sides():
    assert momentum("tvl", None, 5) is None and momentum("tvl", 5, None) is None


def test_weighted_average_ignores_nulls_and_renormalises():
    score, raw, missing, coverage = weighted_score({"volume_reputable": None, "rel_btc": 0.0}, W)
    # all the weight renormalises onto rel_btc -> raw 0, NOT 0*0.15/0.35 as zero-filling gives
    assert raw == pytest.approx(0.0)
    assert coverage == pytest.approx(0.4286, abs=1e-4)
    assert missing == ["volume_reputable"]


def test_unweighted_metrics_never_enter_the_composite():
    """tvl and exchange_count are still collected and stored, but carry no weight,
    so they neither move the score nor count as missing from it."""
    ranks = {"volume_reputable": 80.0, "rel_btc": 80.0, "tvl": 0.0, "exchange_count": 0.0}
    score, raw, missing, coverage = weighted_score(ranks, W)
    assert score == raw == pytest.approx(80.0)
    assert missing == [] and coverage == 1.0


def test_single_weighted_metric_is_pulled_toward_neutral():
    """With two weights (4:3) the factor is exactly 5/7 for either one alone."""
    assert spread_factor(["volume_reputable"], W) == pytest.approx(5 / 7)
    assert spread_factor(["rel_btc"], W) == pytest.approx(5 / 7)
    score, raw, _, _ = weighted_score({"volume_reputable": 100.0, "rel_btc": None}, W)
    assert raw == 100.0 and score == pytest.approx(50 + 50 * 5 / 7)


def test_null_is_never_treated_as_zero():
    full = {k: 80.0 for k in WIDE}
    partial = {**full, "c": None, "d": None}
    # the renormalised average itself is untouched by the missing metrics...
    assert weighted_score(partial, WIDE)[1] == pytest.approx(weighted_score(full, WIDE)[1])
    # ...only the shrinkage toward 50 separates them, and it stays far above zero-filling
    # (0.65 coverage zero-filled would be 80*0.65 == 52)
    assert weighted_score(partial, WIDE)[0] == pytest.approx(74.32, abs=0.01)


def test_weighted_average_all_null():
    score, raw, missing, coverage = weighted_score({k: None for k in W}, W)
    assert score is None and raw is None and coverage == 0.0 and sorted(missing) == sorted(W)


def test_full_coverage_is_unshrunk():
    full = {k: 80.0 for k in WIDE}
    score, raw, _, coverage = weighted_score(full, WIDE)
    assert spread_factor(WIDE, WIDE) == pytest.approx(1.0)
    assert coverage == 1.0 and score == raw == pytest.approx(80.0)


def test_single_metric_is_pulled_substantially_toward_neutral():
    one = {k: None for k in WIDE} | {"c": 100.0}
    score, raw, _, coverage = weighted_score(one, WIDE)
    assert raw == 100.0 and coverage == pytest.approx(0.2)
    assert spread_factor(["c"], WIDE) == pytest.approx(0.4183, abs=1e-4)  # sqrt(0.175)
    assert score == pytest.approx(70.9, abs=0.1)


def test_shrinkage_preserves_order_within_a_coverage_bucket():
    """f depends only on WHICH metrics are present, so two coins sharing a present-set
    keep their relative order -- the fix equalises spread, it does not re-rank."""
    present = ["c", "d", "f"]
    coins = [{k: (v if k in present else None) for k in WIDE}
             for v in (5.0, 27.5, 49.9, 50.0, 50.1, 73.0, 96.0)]
    scored = [weighted_score(c, WIDE) for c in coins]
    raws = [s[1] for s in scored]
    adj = [s[0] for s in scored]
    assert raws == sorted(raws) and adj == sorted(adj)
    assert len(set(adj)) == len(adj)                       # strictly monotone, no collapse to ties
    assert all(abs(a - 50) <= abs(r - 50) + 1e-9 for a, r in zip(adj, raws))


def test_shrinkage_is_monotone_in_coverage():
    """Every added metric narrows the gap to the full-coverage spread, up to f == 1."""
    keys = list(WIDE)
    factors = [spread_factor(keys[:n], WIDE) for n in range(1, len(keys) + 1)]
    assert factors == sorted(factors) and factors[-1] == pytest.approx(1.0)


def test_shrinkage_can_be_switched_off():
    partial = {k: None for k in WIDE} | {"c": 90.0, "d": 90.0}
    assert weighted_score(partial, WIDE, shrink=False)[0] == pytest.approx(90.0)
    assert weighted_score(partial, WIDE, shrink=True)[0] < 90.0


def test_change_pct():
    assert change_pct(150, 100) == pytest.approx(50.0)
    assert change_pct(50, 100) == pytest.approx(-50.0)
    assert change_pct(1, 0) is None and change_pct(None, 5) is None
    assert change_pct(-0.05, -0.10) == pytest.approx(50.0)  # abs() denominator


def test_score_change():
    assert score_change(70.0, 64.0) == pytest.approx(6.0)
    assert score_change(70.0, None) is None   # first run for a coin
    assert score_change(None, 64.0) is None
