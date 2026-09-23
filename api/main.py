import json
import os

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

from truffle import config
from truffle.db import open_db
from truffle.pipeline import METRIC_KEYS

cfg = config.load(os.getenv("TRUFFLE_CONFIG"))
app = FastAPI(title="Truffle API", version="1.0")
app.add_middleware(CORSMiddleware, allow_origins=["http://localhost:5173"],
                   allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

SORT_KEYS = {"score", "score_change", "market_cap", "market_cap_rank", *METRIC_KEYS}
ROW_KEYS = {"score", "score_change", "market_cap", "market_cap_rank"}


def db():
    con = getattr(app.state, "con", None)
    if con is None:
        con = app.state.con = open_db(cfg["db_path"])
    return con


def z(ts):
    return None if not ts else ts.replace("+00:00", "Z") if "+" in ts or ts.endswith("Z") else ts + "Z"


def resolve_run(con, date):
    q = "SELECT * FROM runs WHERE finished_at IS NOT NULL"
    args = ()
    if date:
        q += " AND run_date=?"
        args = (date,)
    row = con.execute(q + " ORDER BY run_date DESC, id DESC LIMIT 1", args).fetchone()
    if row is None:
        if date:
            raise HTTPException(404, f"no run for date {date}")
        return None, None   # empty DB: endpoints answer 200 with an empty list
    prev = con.execute("SELECT run_date FROM runs WHERE run_date<? AND finished_at IS NOT NULL "
                       "ORDER BY run_date DESC LIMIT 1", (row["run_date"],)).fetchone()
    return row, (prev["run_date"] if prev else None)


def metric_map(con, run_id, coin_ids=None):
    q = "SELECT * FROM metrics WHERE run_id=? AND metric_key NOT LIKE '\\_%' ESCAPE '\\'"
    args = [run_id]
    if coin_ids is not None:
        q += f" AND coin_id IN ({','.join('?' * len(coin_ids))})"
        args += list(coin_ids)
    out = {}
    for m in con.execute(q, args):
        out.setdefault(m["coin_id"], {})[m["metric_key"]] = {
            "current": m["current"], "prior": m["prior"],
            "change_pct": m["change_pct"], "rank_score": m["rank_score"]}
    return out


def empty_metrics():
    return {k: {"current": None, "prior": None, "change_pct": None, "rank_score": None} for k in METRIC_KEYS}


def pending_history(missing, mm):
    """The subset of `missing` we do have a current reading for but no prior window yet
    (exchange_count until 30 days of our own history exist). Known, but not yet scoreable."""
    return [k for k in missing if (v := mm.get(k)) and v["current"] is not None and v["prior"] is None]


def coin_rows(con, run_id, coin_ids=None):
    q = ("SELECT u.*, s.score, s.score_raw, s.score_prev, s.score_change, s.missing FROM universe_snapshots u "
         "LEFT JOIN scores s ON s.run_id=u.run_id AND s.coin_id=u.coin_id "
         "WHERE u.run_id=? AND u.included=1")
    args = [run_id]
    if coin_ids is not None:
        q += f" AND u.coin_id IN ({','.join('?' * len(coin_ids))})"
        args += list(coin_ids)
    metrics = metric_map(con, run_id, coin_ids)
    rows = []
    for r in con.execute(q, args):
        mm = {**empty_metrics(), **metrics.get(r["coin_id"], {})}
        missing = (json.loads(r["missing"]) if r["missing"]
                   else [k for k, v in mm.items() if v["current"] is None])
        rows.append({
            "coin_id": r["coin_id"], "symbol": r["symbol"], "name": r["name"], "image": r["image"],
            "market_cap_rank": r["market_cap_rank"], "market_cap": r["market_cap"],
            "price_usd": r["price_usd"], "total_volume": r["total_volume"],
            "score": r["score"], "score_raw": r["score_raw"],
            "score_prev": r["score_prev"], "score_change": r["score_change"],
            "missing": missing, "pending": pending_history(missing, mm),
            "metrics": mm,
        })
    return rows


def sort_rows(rows, sort, order):
    # Metric sorts use rank_score (the momentum percentile) -- the comparable quantity
    # across coins; raw magnitudes would just re-sort by market cap.
    key = (lambda r: r.get(sort)) if sort in ROW_KEYS else (lambda r: r["metrics"][sort]["rank_score"])
    desc = order == "desc"
    return sorted(rows, key=lambda r: (key(r) is None, -key(r) if desc and key(r) is not None else key(r)))


@app.get("/api/health")
def health():
    n = db().execute("SELECT COUNT(*) c FROM runs WHERE finished_at IS NOT NULL").fetchone()["c"]
    return {"status": "ok", "db": os.path.basename(str(cfg["db_path"])), "runs": n}


@app.get("/api/runs")
def runs():
    rows = db().execute("SELECT run_date, started_at, finished_at, coin_count, MAX(id) FROM runs "
                        "WHERE finished_at IS NOT NULL GROUP BY run_date ORDER BY run_date DESC")
    return {"runs": [{"run_date": r["run_date"], "started_at": z(r["started_at"]),
                      "finished_at": z(r["finished_at"]), "coin_count": r["coin_count"]} for r in rows]}


@app.get("/api/coins")
def coins(date: str | None = None, min_volume: float | None = None, sort: str = "score",
          order: str = "desc", limit: int = 300, offset: int = 0, q: str | None = None):
    if sort not in SORT_KEYS:
        raise HTTPException(400, f"bad sort '{sort}'; allowed: {sorted(SORT_KEYS)}")
    if order not in ("asc", "desc"):
        raise HTTPException(400, "order must be 'asc' or 'desc'")
    con = db()
    run, prev = resolve_run(con, date)
    if run is None:
        return {"run_date": None, "prev_run_date": None, "total": 0, "coins": []}
    rows = coin_rows(con, run["id"])
    if min_volume is not None:
        rows = [r for r in rows if (r["total_volume"] or 0) >= min_volume]
    if q:
        needle = q.lower()
        rows = [r for r in rows if needle in (r["name"] or "").lower() or needle in (r["symbol"] or "").lower()]
    total = len(rows)
    rows = sort_rows(rows, sort, order)[offset:offset + limit]
    return {"run_date": run["run_date"], "prev_run_date": prev, "total": total, "coins": rows}


@app.get("/api/truffles")
def truffles(date: str | None = None, limit: int = 10, min_volume: float | None = None):
    con = db()
    run, prev = resolve_run(con, date)
    if run is None:
        return {"run_date": None, "prev_run_date": None, "truffles": []}
    if prev is None:
        return {"run_date": run["run_date"], "prev_run_date": None, "truffles": []}
    rows = [r for r in coin_rows(con, run["id"]) if (r["score_change"] or 0) > 0]
    if min_volume is not None:
        rows = [r for r in rows if (r["total_volume"] or 0) >= min_volume]
    rows.sort(key=lambda r: -r["score_change"])
    return {"run_date": run["run_date"], "prev_run_date": prev, "truffles": rows[:limit]}


@app.get("/api/coins/{coin_id}")
def coin_detail(coin_id: str, date: str | None = None):
    con = db()
    c = con.execute("SELECT * FROM coins WHERE coin_id=?", (coin_id,)).fetchone()
    if c is None:
        raise HTTPException(404, f"unknown coin '{coin_id}'")
    run, _ = resolve_run(con, date)
    latest = next(iter(coin_rows(con, run["id"], [coin_id])), None) if run else None

    # history stops at the selected run, so an older run never charts data from its future
    history = []
    for r in con.execute("SELECT r.id, r.run_date, s.score, s.score_change FROM scores s "
                         "JOIN runs r ON r.id=s.run_id WHERE s.coin_id=? AND r.finished_at IS NOT NULL "
                         "AND (? IS NULL OR r.run_date<=?) ORDER BY r.run_date ASC, r.id ASC",
                         (coin_id, date, date)):
        mm = metric_map(con, r["id"], [coin_id]).get(coin_id, {})
        history.append({"run_date": r["run_date"], "score": r["score"], "score_change": r["score_change"],
                        "metrics": {**empty_metrics(), **mm}})
    return {
        "coin": {"coin_id": c["coin_id"], "symbol": c["symbol"], "name": c["name"], "image": c["image"],
                 "categories": json.loads(c["categories"] or "[]"),
                 "github_repos": json.loads(c["github_repos"] or "[]"),
                 "defillama_slug": c["defillama_slug"], "homepage": c["homepage"],
                 "metadata_updated_at": z(c["metadata_updated_at"])},
        "latest": latest, "history": history,
    }
