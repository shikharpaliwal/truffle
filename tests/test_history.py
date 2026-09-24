import json
from datetime import datetime, timezone

import pytest

from truffle.db import open_db
from truffle.pipeline import prior_from_history, previous_score
from truffle.scoring import momentum, weighted_score

WEIGHTS = {"volume_reputable": 0.2, "rel_btc": 0.15}          # the production composite
# exchange_count is collected but unweighted; this map is what the pending mechanism
# looks like for any metric whose prior comes from our own storage AND carries weight.
WEIGHTS_WITH_HISTORY_METRIC = {**WEIGHTS, "exchange_count": 0.1}


@pytest.fixture
def con(tmp_path):
    return open_db(tmp_path / "t.db")


def add_run(con, date):
    return con.execute("INSERT INTO runs(run_date,started_at,finished_at) VALUES(?,?,?)",
                       (date, f"{date}T00:00:00+00:00", f"{date}T01:00:00+00:00")).lastrowid


def add_score(con, run_id, coin, score, prev=None):
    con.execute("INSERT INTO scores(run_id,coin_id,score,score_prev,score_change,missing) VALUES(?,?,?,?,?,?)",
                (run_id, coin, score, prev, None if prev is None else score - prev, json.dumps([])))


def add_metric(con, run_id, coin, key, cur):
    con.execute("INSERT INTO metrics(run_id,coin_id,metric_key,current) VALUES(?,?,?,?)", (run_id, coin, key, cur))


def test_first_run_has_no_previous_score(con):
    r = add_run(con, "2026-09-01")
    add_score(con, r, "solana", 60.0)
    assert previous_score(con, "solana", "2026-09-01") is None


def test_previous_score_picks_the_most_recent_earlier_run(con):
    for d, s in [("2026-09-01", 50.0), ("2026-09-08", 61.0)]:
        add_score(con, add_run(con, d), "solana", s)
    add_run(con, "2026-09-15")
    assert previous_score(con, "solana", "2026-09-15") == 61.0


def test_previous_score_is_coin_scoped(con):
    add_score(con, add_run(con, "2026-09-01"), "solana", 50.0)
    add_run(con, "2026-09-08")
    assert previous_score(con, "cardano", "2026-09-08") is None


def test_rerun_of_same_date_is_not_history(con):
    """Re-runs share a date but not a universe; percentile scores across them are incomparable."""
    add_score(con, add_run(con, "2026-09-15"), "solana", 40.0)
    add_run(con, "2026-09-15")
    assert previous_score(con, "solana", "2026-09-15") is None


def test_rerun_of_same_date_still_sees_an_earlier_date(con):
    add_score(con, add_run(con, "2026-09-08"), "solana", 30.0)
    add_score(con, add_run(con, "2026-09-15"), "solana", 40.0)
    add_run(con, "2026-09-15")
    assert previous_score(con, "solana", "2026-09-15") == 30.0


AS_OF = datetime(2026, 10, 1, tzinfo=timezone.utc)


def test_prior_from_history_null_until_history_exists(con):
    add_metric(con, add_run(con, "2026-09-28"), "solana", "exchange_count", 40)
    # only 3 days old -- nothing at or before the 30-day mark yet
    assert prior_from_history(con, "solana", "exchange_count", AS_OF, 30) is None


def test_prior_from_history_reads_the_30_day_old_value(con):
    add_metric(con, add_run(con, "2026-08-25"), "solana", "exchange_count", 30)
    add_metric(con, add_run(con, "2026-08-31"), "solana", "exchange_count", 35)
    add_metric(con, add_run(con, "2026-09-28"), "solana", "exchange_count", 41)
    assert prior_from_history(con, "solana", "exchange_count", AS_OF, 30) == 35


def test_prior_from_history_skips_nulls(con):
    add_metric(con, add_run(con, "2026-08-20"), "solana", "exchange_count", 28)
    add_metric(con, add_run(con, "2026-08-31"), "solana", "exchange_count", None)
    assert prior_from_history(con, "solana", "exchange_count", AS_OF, 30) == 28


def test_exchange_count_has_no_momentum_until_history_exists(con):
    """A current reading with no prior window yields no momentum and so no rank_score.
    It carries no weight either, so it cannot reach the composite by any route."""
    add_metric(con, add_run(con, "2026-09-28"), "solana", "exchange_count", 40)
    prior = prior_from_history(con, "solana", "exchange_count", AS_OF, 30)
    assert prior is None
    assert momentum("exchange_count", 40, prior) is None
    ranks = {k: 50.0 for k in WEIGHTS} | {"exchange_count": None}
    assert weighted_score(ranks, WEIGHTS) == (50.0, 50.0, [], 1.0)


def test_a_weighted_history_metric_stays_out_of_the_score_while_pending(con):
    """The pending mechanism itself: no prior window means no rank, and the weight drops
    out of the coverage rather than being silently counted as present."""
    w = WEIGHTS_WITH_HISTORY_METRIC
    ranks = {k: 50.0 for k in w} | {"exchange_count": None}
    score, raw, missing, coverage = weighted_score(ranks, w)
    assert missing == ["exchange_count"]
    assert coverage == pytest.approx(0.35 / 0.45, abs=1e-4) and score == raw == 50.0


def test_reputable_share_prior_comes_from_history_once_it_exists(con):
    """_reputable_share is an input to volume_reputable, not a scored metric: until a prior
    share is stored the pipeline scales the prior volume window by today's share instead."""
    add_metric(con, add_run(con, "2026-09-28"), "solana", "_reputable_share", 0.8)
    assert prior_from_history(con, "solana", "_reputable_share", AS_OF, 30) is None
    add_metric(con, add_run(con, "2026-08-31"), "solana", "_reputable_share", 0.55)
    assert prior_from_history(con, "solana", "_reputable_share", AS_OF, 30) == 0.55
