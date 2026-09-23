import json

import pytest
from fastapi.testclient import TestClient

from api import main as apimod
from truffle.db import open_db
from truffle.pipeline import METRIC_KEYS

COINS = [("solana", "SOL", "Solana", 6), ("cardano", "ADA", "Cardano", 12), ("aave", "AAVE", "Aave", 40)]
MISSING = {"cardano": ["tvl"], "aave": ["exchange_count"]}


def build(tmp_path):
    con = open_db(tmp_path / "t.db")
    for i, (d, score_base) in enumerate([("2026-09-20", 40.0), ("2026-09-22", 55.0)]):
        run_id = con.execute("INSERT INTO runs(run_date,started_at,finished_at,coin_count)"
                             " VALUES(?,?,?,?)", (d, f"{d}T00:00:00+00:00", f"{d}T00:30:00+00:00", 3)).lastrowid
        for j, (cid, sym, name, rank) in enumerate(COINS):
            con.execute("INSERT INTO universe_snapshots(run_id,coin_id,market_cap_rank,market_cap,price_usd,"
                        "total_volume,symbol,name,image,included) VALUES(?,?,?,?,?,?,?,?,?,1)",
                        (run_id, cid, rank, 1e10 / (j + 1), 10.0 * (j + 1), 1e8 * (j + 1), sym, name, f"http://img/{cid}"))
            con.execute("INSERT INTO coins(coin_id,symbol,name,image,categories,github_repos,defillama_slug,"
                        "homepage,metadata_updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                        (cid, sym, name, f"http://img/{cid}", '["L1"]', f'["{cid}/{cid}"]', cid,
                         f"http://{cid}.org", "2026-09-01T00:00:00+00:00")) if i == 0 else None
            for k in METRIC_KEYS:
                null = (k == "tvl" and cid == "cardano")          # never collected: no protocol
                pend = (k == "exchange_count" and cid == "aave")  # collected, but no prior window yet
                con.execute("INSERT INTO metrics(run_id,coin_id,metric_key,current,prior,change_pct,momentum,"
                            "rank_score) VALUES(?,?,?,?,?,?,?,?)",
                            (run_id, cid, k, None if null else 100.0 + j, None if (null or pend) else 90.0,
                             None if (null or pend) else 11.1, None if (null or pend) else 0.1,
                             None if (null or pend) else 100.0 - 25 * j))
            prev = None if i == 0 else 40.0 - j   # run 1 score, keeps the fixture self-consistent
            s = score_base - j
            con.execute("INSERT INTO scores(run_id,coin_id,score,score_raw,score_prev,score_change,"
                        "missing,weight_coverage) VALUES(?,?,?,?,?,?,?,?)",
                        (run_id, cid, s, s + 2.0, prev, None if prev is None else s - prev,
                         json.dumps(MISSING.get(cid, [])), 0.9 if MISSING.get(cid) else 1.0))
        # internal metric key must never surface in the API
        con.execute("INSERT INTO metrics(run_id,coin_id,metric_key,current) VALUES(?,?,?,?)",
                    (run_id, "solana", "_reputable_share", 0.8))
    con.commit()
    return con


@pytest.fixture
def client(tmp_path):
    apimod.app.state.con = build(tmp_path)
    apimod.cfg["db_path"] = tmp_path / "truffle.db"
    return TestClient(apimod.app)


def test_health(client):
    assert client.get("/api/health").json() == {"status": "ok", "db": "truffle.db", "runs": 2}


def test_runs_newest_first(client):
    r = client.get("/api/runs").json()["runs"]
    assert [x["run_date"] for x in r] == ["2026-09-22", "2026-09-20"]
    assert r[0]["started_at"].endswith("Z") and r[0]["finished_at"].endswith("Z")
    assert r[0]["coin_count"] == 3


def test_coins_envelope_and_row_shape(client):
    d = client.get("/api/coins").json()
    assert set(d) == {"run_date", "prev_run_date", "total", "coins"}
    assert d["run_date"] == "2026-09-22" and d["prev_run_date"] == "2026-09-20" and d["total"] == 3
    row = d["coins"][0]
    assert set(row) == {"coin_id", "symbol", "name", "image", "market_cap_rank", "market_cap", "price_usd",
                        "total_volume", "score", "score_raw", "score_prev", "score_change",
                        "missing", "pending", "metrics"}
    assert set(row["metrics"]) == set(METRIC_KEYS)
    assert set(row["metrics"]["tvl"]) == {"current", "prior", "change_pct", "rank_score"}


def test_pending_history_is_a_flagged_subset_of_missing(client):
    """exchange_count is collected but has no prior window yet: still missing from the score,
    but distinguishable from a metric we could not collect at all."""
    rows = {c["coin_id"]: c for c in client.get("/api/coins").json()["coins"]}
    aave = rows["aave"]
    assert aave["pending"] == ["exchange_count"]
    assert "exchange_count" in aave["missing"]                      # did not contribute to the score
    assert aave["metrics"]["exchange_count"]["current"] is not None  # but we do have a reading
    assert aave["metrics"]["exchange_count"]["rank_score"] is None


def test_unavailable_metric_is_missing_but_not_pending(client):
    cardano = {c["coin_id"]: c for c in client.get("/api/coins").json()["coins"]}["cardano"]
    assert cardano["missing"] == ["tvl"] and cardano["pending"] == []


def test_fully_scored_coin_has_nothing_pending(client):
    solana = {c["coin_id"]: c for c in client.get("/api/coins").json()["coins"]}["solana"]
    assert solana["missing"] == [] and solana["pending"] == []


def test_coin_detail_and_truffles_carry_pending(client):
    assert client.get("/api/coins/aave").json()["latest"]["pending"] == ["exchange_count"]
    assert all("pending" in t for t in client.get("/api/truffles").json()["truffles"])


def test_internal_metric_keys_are_hidden(client):
    row = client.get("/api/coins").json()["coins"][0]
    assert not any(k.startswith("_") for k in row["metrics"])


def test_default_sort_is_score_desc(client):
    ids = [c["coin_id"] for c in client.get("/api/coins").json()["coins"]]
    assert ids == ["solana", "cardano", "aave"]


def test_sort_order_and_metric_sort(client):
    asc = [c["coin_id"] for c in client.get("/api/coins?order=asc").json()["coins"]]
    assert asc == ["aave", "cardano", "solana"]
    by_tvl = client.get("/api/coins?sort=tvl").json()["coins"]
    assert by_tvl[-1]["coin_id"] == "cardano"   # null rank_score sorts last


def test_nulls_sort_last_in_both_directions(client):
    for order in ("asc", "desc"):
        rows = client.get(f"/api/coins?sort=tvl&order={order}").json()["coins"]
        assert rows[-1]["metrics"]["tvl"]["rank_score"] is None


def test_filters(client):
    assert client.get("/api/coins?q=sol").json()["total"] == 1
    assert client.get("/api/coins?q=ADA").json()["coins"][0]["coin_id"] == "cardano"
    assert client.get("/api/coins?min_volume=150000000").json()["total"] == 2


def test_limit_offset_keeps_total_unfiltered(client):
    d = client.get("/api/coins?limit=1&offset=1").json()
    assert d["total"] == 3 and len(d["coins"]) == 1 and d["coins"][0]["coin_id"] == "cardano"


def test_date_param_and_first_run_has_null_score_change(client):
    d = client.get("/api/coins?date=2026-09-20").json()
    assert d["run_date"] == "2026-09-20" and d["prev_run_date"] is None
    assert all(c["score_prev"] is None and c["score_change"] is None for c in d["coins"])


def test_bad_sort_and_order_are_400(client):
    assert client.get("/api/coins?sort=nope").status_code == 400
    assert client.get("/api/coins?order=sideways").status_code == 400


def test_unknown_date_is_404(client):
    r = client.get("/api/coins?date=1999-01-01")
    assert r.status_code == 404 and "detail" in r.json()


def test_coin_detail(client):
    d = client.get("/api/coins/solana").json()
    assert set(d) == {"coin", "latest", "history"}
    c = d["coin"]
    assert c["coin_id"] == "solana" and c["categories"] == ["L1"] and c["github_repos"] == ["solana/solana"]
    assert c["defillama_slug"] == "solana" and c["metadata_updated_at"].endswith("Z")
    assert d["latest"]["coin_id"] == "solana"
    assert [h["run_date"] for h in d["history"]] == ["2026-09-20", "2026-09-22"]   # ascending
    assert set(d["history"][0]["metrics"]) == set(METRIC_KEYS)
    assert d["history"][0]["score_change"] is None and d["history"][1]["score_change"] == 15.0


def test_coin_detail_history_stops_at_the_selected_date(client):
    d = client.get("/api/coins/solana?date=2026-09-20").json()
    assert [h["run_date"] for h in d["history"]] == ["2026-09-20"]
    assert d["latest"]["coin_id"] == "solana"


def test_unknown_coin_is_404(client):
    assert client.get("/api/coins/not-a-coin").status_code == 404


def test_truffles(client):
    d = client.get("/api/truffles").json()
    assert set(d) == {"run_date", "prev_run_date", "truffles"}
    changes = [t["score_change"] for t in d["truffles"]]
    assert changes == sorted(changes, reverse=True)
    assert all(c is not None for c in changes)


def test_truffles_excludes_null_score_change(client):
    assert client.get("/api/truffles?date=2026-09-20").json()["truffles"] == []


def test_truffles_empty_without_a_previous_run(client):
    d = client.get("/api/truffles?date=2026-09-20").json()
    assert d["run_date"] == "2026-09-20" and d["prev_run_date"] is None and d["truffles"] == []


def test_truffles_excludes_fallers(client):
    con = apimod.app.state.con
    run_id = con.execute("SELECT MAX(id) i FROM runs").fetchone()["i"]
    con.execute("UPDATE scores SET score_prev=score+5, score_change=-5 WHERE run_id=? AND coin_id!='solana'",
                (run_id,))
    con.commit()
    ids = [t["coin_id"] for t in client.get("/api/truffles").json()["truffles"]]
    assert ids == ["solana"]


def test_truffles_limit(client):
    assert len(client.get("/api/truffles?limit=1").json()["truffles"]) == 1


def test_cors_allows_the_frontend_origin(client):
    r = client.get("/api/health", headers={"Origin": "http://localhost:5173"})
    assert r.headers["access-control-allow-origin"] == "http://localhost:5173"


def test_empty_db_is_200_with_empty_lists(tmp_path):
    apimod.app.state.con = open_db(tmp_path / "empty.db")
    c = TestClient(apimod.app, raise_server_exceptions=False)
    coins = c.get("/api/coins")
    assert coins.status_code == 200
    assert coins.json() == {"run_date": None, "prev_run_date": None, "total": 0, "coins": []}
    truffles = c.get("/api/truffles")
    assert truffles.status_code == 200 and truffles.json()["truffles"] == []
    assert c.get("/api/runs").json() == {"runs": []}
    assert c.get("/api/health").json()["runs"] == 0
