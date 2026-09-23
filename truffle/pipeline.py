import json
import logging
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

from . import metadata, scoring
from .cache import Cache
from .db import add_usage, jload, open_db, usage_this_month
from .http import HttpClient
from .metrics import exchanges as ex
from .metrics import github as gh
from .metrics import market as mk
from .metrics import tvl as llama
from .universe import build as build_universe

log = logging.getLogger(__name__)
METRIC_KEYS = ["volume_reputable", "tvl", "contributors", "commits", "exchange_count", "rel_btc"]
SHARE_KEY = "_reputable_share"  # internal: lets a later run reconstruct a true prior-window share
CG_HOST = "api.coingecko.com"


def _utc(d):
    return datetime.fromisoformat(d).replace(tzinfo=timezone.utc)


def _next_month_start(now=None):
    now = now or datetime.now(timezone.utc)
    return f"{now.year + (now.month == 12)}-{now.month % 12 + 1:02d}-01"


def prior_from_history(con, coin_id, key, as_of, window_days, field="current"):
    """Metrics with no historical API (exchange_count, reputable share) read their prior
    value from our own storage ~1 window ago; null until that history exists."""
    target = (as_of - timedelta(days=window_days)).date().isoformat()
    row = con.execute(
        f"SELECT m.{field} v FROM metrics m JOIN runs r ON r.id=m.run_id "
        "WHERE m.coin_id=? AND m.metric_key=? AND r.run_date<=? "
        f"AND m.{field} IS NOT NULL ORDER BY r.run_date DESC, r.id DESC LIMIT 1",
        (coin_id, key, target)).fetchone()
    return row["v"] if row else None


def previous_score(con, coin_id, run_date):
    """The latest score from a strictly earlier run_date. Same-date re-runs are not history:
    scores are percentile ranks within a run's universe, so they are only comparable across dates."""
    row = con.execute(
        "SELECT s.score FROM scores s JOIN runs r ON r.id=s.run_id "
        "WHERE s.coin_id=? AND r.run_date<? AND s.score IS NOT NULL "
        "ORDER BY r.run_date DESC, r.id DESC LIMIT 1", (coin_id, run_date)).fetchone()
    return row["score"] if row else None


class Pipeline:
    def __init__(self, cfg, args):
        self.cfg, self.args = cfg, args
        self.window = cfg["window_days"]
        self.con = open_db(cfg["db_path"])
        self.cache = Cache(cfg["cache_dir"], cfg["cache"]["ttl_seconds"], enabled=not args.refresh)
        self.http = HttpClient(cfg, self.cache)
        self.errors = []

    def record_error(self, coin_id, stage, msg):
        self.errors.append((coin_id, stage, str(msg)[:300]))
        log.warning("[%s] %s failed: %s", coin_id or "-", stage, str(msg)[:200])

    def run(self):
        run_date = self.args.date or datetime.now(timezone.utc).date().isoformat()
        as_of = _utc(run_date) + timedelta(days=1)  # windows end at the close of run_date
        limit = self.args.limit or self.cfg["universe_size"]
        started = datetime.now(timezone.utc).isoformat()

        if not self.check_budget(limit):
            return None
        try:
            return self.execute(run_date, as_of, limit, started)
        finally:
            self.record_usage()

    def execute(self, run_date, as_of, limit, started):
        kept, snapshot = build_universe(self.http, self.cfg, limit)
        if self.args.dry_run:
            self.report_projection(len(kept))
            log.info("dry run: %d coins would be scored; nothing written", len(kept))
            return None

        cur = self.con.execute("INSERT INTO runs(run_date,started_at,coin_count) VALUES(?,?,?)",
                               (run_date, started, len(kept)))
        run_id = cur.lastrowid
        self.con.executemany(
            "INSERT OR REPLACE INTO universe_snapshots(run_id,coin_id,market_cap_rank,market_cap,price_usd,"
            "total_volume,symbol,name,image,included,exclusion_reason) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            [(run_id, r["id"], r.get("market_cap_rank"), r.get("market_cap"), r.get("current_price"),
              r.get("total_volume"), (r.get("symbol") or "").upper(), r.get("name"), r.get("image"),
              int(r["included"]), r["exclusion_reason"]) for r in snapshot])
        self.con.commit()

        trust = ex.trust_table(self.http)
        slugs = self.safe(lambda: llama.slug_map(self.http), None, "defillama_slugs") or {}
        btc = self.safe(lambda: mk.fetch_chart(self.http, "bitcoin", self.window), None, "btc_chart") or {}
        btc_prices = btc.get("prices") or []
        if not btc_prices:
            log.error("no BTC price series: rel_btc will be null for every coin")

        raw = {}
        for i, row in enumerate(kept, 1):
            cid = row["id"]
            log.info("[%d/%d] %s", i, len(kept), cid)
            raw[cid] = self.collect(run_id, cid, row, trust, slugs, btc_prices, as_of)

        self.finish(run_id, run_date, kept, raw, started)
        return run_id

    def safe(self, fn, coin_id, stage):
        try:
            return fn()
        except Exception as e:  # one coin or one API must never abort the run
            self.record_error(coin_id, stage, e)
            return None

    def collect(self, run_id, cid, market_row, trust, slugs, btc_prices, as_of):
        cfg, w = self.cfg, self.window
        vals = {k: (None, None) for k in METRIC_KEYS}
        extra = {}

        row = self.con.execute("SELECT * FROM coins WHERE coin_id=?", (cid,)).fetchone()
        repos = jload(row["github_repos"]) if row else []
        slug = row["defillama_slug"] if row else None
        refresh_meta = not self.args.skip_metadata and metadata.needs_refresh(row, cfg["metadata"]["refresh_days"])
        if refresh_meta:
            detail = self.safe(lambda: metadata.fetch(self.http, cid), cid, "metadata")
            if detail is not None:
                slug = slugs.get(cid)
                repos = metadata.upsert(self.con, cid, market_row, detail, slug, cfg["github"]["max_repos_per_coin"])
            else:
                metadata.touch(self.con, cid, market_row)
        else:
            metadata.touch(self.con, cid, market_row)

        chart = self.safe(lambda: mk.fetch_chart(self.http, cid, w), cid, "market_chart") or {}
        prices, vols = chart.get("prices") or [], chart.get("total_volumes") or []
        if prices and btc_prices:
            vals["rel_btc"] = mk.rel_btc(prices, btc_prices, w)

        tickers = self.safe(lambda: ex.fetch_tickers(self.http, cid), cid, "tickers") or []
        share = ex.reputable_share(tickers, trust) if tickers else None
        if share is not None:
            extra[SHARE_KEY] = share
        ec = ex.exchange_count(tickers, trust)
        if ec is not None:
            vals["exchange_count"] = (ec, prior_from_history(self.con, cid, "exchange_count", as_of, w))

        # Volume history is only available in total; scale each window by the reputable
        # share observed at that time (prior share from our own history, else today's).
        v_cur, v_pri = mk.window_sum(vols, w), mk.window_sum(vols, w, prior=True)
        if share is not None and v_cur is not None:
            pri_share = prior_from_history(self.con, cid, SHARE_KEY, as_of, w) or share
            vals["volume_reputable"] = (v_cur * share, v_pri * pri_share if v_pri is not None else None)

        if slug:
            t = self.safe(lambda: llama.fetch_tvl(self.http, slug, w), cid, "tvl")
            if t:
                vals["tvl"] = t

        if repos:
            for repo in repos:
                ok, note = self.safe(
                    lambda r=repo: gh.sync_repo(self.con, self.http, r, cid, cfg["github"]["commit_lookback_days"],
                                                cfg["github"]["min_repo_stars"], refresh_meta),
                    cid, f"github:{repo}") or (False, "skipped")
                log.debug("  repo %s: %s", repo, note)
            usable = gh.usable_repos(self.con, cid)
            (c_cur, c_pri), (a_cur, a_pri) = gh.window_stats(self.con, usable, as_of, w)
            if usable:
                vals["commits"] = (c_cur, c_pri)
                vals["contributors"] = (a_cur, a_pri)
        self.con.commit()
        return {"values": vals, "extra": extra}

    def finish(self, run_id, run_date, kept, raw, started):
        cfg = self.cfg
        moms = {k: {} for k in METRIC_KEYS}
        for cid, d in raw.items():
            for k, (c, p) in d["values"].items():
                moms[k][cid] = scoring.momentum(k, c, p)
        ranks = {k: scoring.rank_scores(v) for k, v in moms.items()}

        rows, score_rows, missing_counter = [], [], Counter()
        for cid, d in raw.items():
            for k, (c, p) in d["values"].items():
                rows.append((run_id, cid, k, c, p, scoring.change_pct(c, p), moms[k][cid], ranks[k][cid]))
            for k, v in d["extra"].items():
                rows.append((run_id, cid, k, v, None, None, None, None))
            per_coin = {k: ranks[k][cid] for k in METRIC_KEYS}
            score, raw, missing, coverage = scoring.weighted_score(
                per_coin, cfg["weights"], cfg.get("scoring", {}).get("variance_shrinkage", True))
            prev = previous_score(self.con, cid, run_date)
            score_rows.append((run_id, cid, score, raw, prev, scoring.score_change(score, prev),
                               json.dumps(missing), coverage))
            missing_counter.update(missing)
            if missing:
                log.debug("missing for %s: %s", cid, ",".join(missing))

        self.con.executemany("INSERT OR REPLACE INTO metrics(run_id,coin_id,metric_key,current,prior,"
                             "change_pct,momentum,rank_score) VALUES(?,?,?,?,?,?,?,?)", rows)
        self.con.executemany("INSERT OR REPLACE INTO scores(run_id,coin_id,score,score_raw,score_prev,"
                             "score_change,missing,weight_coverage) VALUES(?,?,?,?,?,?,?,?)", score_rows)
        self.con.executemany("INSERT INTO run_errors(run_id,coin_id,stage,message) VALUES(?,?,?,?)",
                             [(run_id, *e) for e in self.errors])
        self.con.execute("UPDATE runs SET finished_at=?, coin_count=? WHERE id=?",
                         (datetime.now(timezone.utc).isoformat(), len(kept), run_id))
        self.con.commit()
        self.summary(run_id, len(kept), missing_counter, started)

    def summary(self, run_id, n, missing, started):
        elapsed = (datetime.now(timezone.utc) - _utc(started)).total_seconds()
        log.info("=" * 58)
        log.info("run %d: %d coins in %.0fs", run_id, n, elapsed)
        log.info("%-20s %8s %8s", "metric", "missing", "present")
        for k in METRIC_KEYS:
            log.info("%-20s %8d %8d", k, missing[k], n - missing[k])
        scored = self.con.execute("SELECT COUNT(*) c FROM scores WHERE run_id=? AND score IS NOT NULL",
                                  (run_id,)).fetchone()["c"]
        changed = self.con.execute("SELECT COUNT(*) c FROM scores WHERE run_id=? AND score_change IS NOT NULL",
                                   (run_id,)).fetchone()["c"]
        log.info("scored=%d  score_change non-null=%d  errors=%d", scored, changed, len(self.errors))
        log.info("http: %s", self.http.stats())
        if self.errors:
            by_stage = Counter(s for _, s, _ in self.errors)
            log.info("errors by stage: %s", dict(by_stage))
        log.info("=" * 58)

    def cg_projection(self, n):
        """CoinGecko calls a run of n coins costs: fixed setup + chart/tickers/metadata per coin."""
        return 1 + len(self.cfg["exclusions"]["categories"]) + 2 + n * 3

    def check_budget(self, n):
        budget = (self.cfg["http"].get("monthly_budget") or {}).get(CG_HOST)
        if not budget:
            return True
        used = usage_this_month(self.con, CG_HOST)
        remaining, proj = budget - used, self.cg_projection(n)
        log.info("coingecko credits: %d/%d used this month, %d remaining; this run projects ~%d",
                 used, budget, remaining, proj)
        if used >= 0.8 * budget:
            log.warning("over 80%% of the monthly CoinGecko budget is spent (%d of %d)", used, budget)
        if proj > remaining and not self.args.ignore_budget:
            log.error("aborting: ~%d credits needed but only %d remain until the budget resets on %s. "
                      "Re-run with --ignore-budget to proceed anyway.", proj, remaining, _next_month_start())
            return False
        return True

    def record_usage(self):
        for host, n in self.http.per_host.items():
            add_usage(self.con, host, n)
        self.con.commit()

    def report_projection(self, n):
        """Projected call count and wall time at the configured per-host rates."""
        h = self.cfg["http"]["hosts"]
        cg_rate = h["api.coingecko.com"]["rate_per_min"]
        cg = 1 + len(self.cfg["exclusions"]["categories"]) + 2 + n * 2  # markets+cats+exchanges+btc + chart/tickers
        meta = n  # /coins/{id} on first run or a 30-day refresh
        gh_calls = n * self.cfg["github"]["max_repos_per_coin"] * 2
        log.info("projection for %d coins:", n)
        log.info("  coingecko: %d calls (+%d metadata on a cold run) @ %d/min -> %.1f min",
                 cg, meta, cg_rate, (cg + meta) / cg_rate)
        log.info("  defillama: ~%d calls (1 protocols listing + 1 per coin with a slug)", n + 1)
        log.info("  github:    <=%d calls; unauthenticated budget is 60/hr, a token gives 5000/hr", gh_calls)
