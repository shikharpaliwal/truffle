import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from . import binance, symbols
from .binance import HOUR_MS, PAGE
from .cache import Cache
from .db import add_usage, open_db
from .http import HttpClient

log = logging.getLogger(__name__)

UPSERT_BAR = ("INSERT INTO klines_1h(symbol,market,open_time,open,high,low,close,volume,"
              "quote_volume,trades) VALUES(?,?,?,?,?,?,?,?,?,?) "
              "ON CONFLICT(symbol,market,open_time) DO UPDATE SET open=excluded.open,"
              "high=excluded.high,low=excluded.low,close=excluded.close,volume=excluded.volume,"
              "quote_volume=excluded.quote_volume,trades=excluded.trades")
UPSERT_FUNDING = ("INSERT INTO funding_rates(symbol,funding_time,funding_rate,mark_price) "
                  "VALUES(?,?,?,?) ON CONFLICT(symbol,funding_time) DO UPDATE SET "
                  "funding_rate=excluded.funding_rate, mark_price=excluded.mark_price")


def floor_hour(ms):
    return ms - ms % HOUR_MS


def due(last, floor_ms, now_ms):
    """Open time of the first bar we still need, or None when the next one has not closed.

    A bar opening at t is only fetchable once t+1h has passed, so re-running inside the
    same hour asks for nothing. With no history at all we start at `floor_ms`.
    """
    start = floor_hour(floor_ms) if last is None else last + HOUR_MS
    return start if start + HOUR_MS <= now_ms else None


class Collector:
    def __init__(self, cfg, args):
        self.cfg, self.args, self.b = cfg, args, cfg["binance"]
        self.con = open_db(cfg["db_path"])
        # Binance payloads are large and single-use; nothing here goes through the disk cache.
        self.http = HttpClient(cfg, Cache(cfg["cache_dir"], None, enabled=False))
        self.failures = {}

    # --- symbol map -------------------------------------------------------

    def remap(self):
        coins = symbols.universe_coins(self.con)
        if not coins:
            log.error("no finished run in the database; run run.py first")
            return None
        m = self.b["mapping"]
        rows = []
        for market in self.b["markets"]:
            pairs = [{"symbol": s["symbol"], "market": market, "baseAsset": s["baseAsset"]}
                     for s in binance.usdt_symbols(self.http, market)]
            prices = binance.usdt_prices(self.http, market)
            rows += symbols.resolve(coins, pairs, prices, m["price_tolerance"], m["multiplier_prefixes"])
            log.info("%s: %d USDT pairs, %d priced", market, len(pairs), len(prices))
        kept_manual = symbols.store(self.con, rows)
        s = symbols.summary(self.con)
        log.info("symbol map over %d universe coins: %s (%d manual rows preserved)",
                 len(coins), s, kept_manual)
        for r in symbols.ambiguous(self.con):
            log.warning("ambiguous %-16s %-5s %s", r["symbol"], r["market"], r["note"])
        return s

    def ensure_map(self):
        if not self.con.execute("SELECT 1 FROM binance_symbols LIMIT 1").fetchone():
            log.info("no symbol map yet; building one")
            self.remap()

    # --- bars -------------------------------------------------------------

    def last_bar(self, symbol, market):
        return self.con.execute("SELECT MAX(open_time) t FROM klines_1h WHERE symbol=? AND market=?",
                                (symbol, market)).fetchone()["t"]

    def fetch_bars(self, symbol, market, start_ms, now_ms):
        """Page forward from start_ms. A missed run of any length is just more pages."""
        out, cursor, page = [], start_ms, PAGE[market]
        while cursor < now_ms:
            rows = binance.klines(self.http, symbol, market, start_ms=cursor)
            if not rows:
                break
            bars = [binance.parse_kline(r) for r in rows]
            out += [b for b in bars if b[8] < now_ms]     # closed bars only
            if len(rows) < page:
                break
            cursor = bars[-1][0] + HOUR_MS
        return out

    def _bar_job(self, symbol, market, start_ms, now_ms):
        try:
            bars = self.fetch_bars(symbol, market, start_ms, now_ms)
        except Exception as e:
            return symbol, market, None, str(e)[:200]
        return symbol, market, bars, None

    def collect_bars(self, tracked, floor_ms, now_ms):
        jobs = []
        for t in tracked:
            start = due(self.last_bar(t["symbol"], t["market"]), floor_ms, now_ms)
            if start is not None:
                jobs.append((t["symbol"], t["market"], start, now_ms))
        log.info("%d of %d tracked symbols are due for bars", len(jobs), len(tracked))
        fetched = 0
        with ThreadPoolExecutor(max_workers=self.b["workers"]) as pool:
            for i, (sym, market, bars, err) in enumerate(pool.map(lambda a: self._bar_job(*a), jobs), 1):
                if err:
                    self.failures[f"{market}:{sym}"] = err
                    log.warning("%s %s: %s", market, sym, err)
                    continue
                self.con.executemany(UPSERT_BAR, [(sym, market, *b[:8]) for b in bars])
                fetched += len(bars)
                if i % 50 == 0 or i == len(jobs):
                    self.con.commit()
                    log.info("  [%d/%d] %d bars", i, len(jobs), fetched)
        self.con.commit()
        return fetched

    # --- funding ----------------------------------------------------------

    def collect_funding(self, tracked, floor_ms, now_ms):
        perps = [t["symbol"] for t in tracked if t["market"] == "perp"]
        jobs = []
        for sym in perps:
            # Funding settles every 1h, 4h or 8h depending on the contract; infer the
            # cadence from what we already hold so a re-run inside it costs nothing.
            seen = [r["t"] for r in self.con.execute(
                "SELECT funding_time t FROM funding_rates WHERE symbol=? ORDER BY t DESC LIMIT 2", (sym,))]
            if not seen:
                jobs.append((sym, floor_hour(floor_ms)))
            elif now_ms >= seen[0] + (seen[0] - seen[1] if len(seen) > 1 else HOUR_MS):
                jobs.append((sym, seen[0] + 1))
        fetched = 0
        with ThreadPoolExecutor(max_workers=self.b["workers"]) as pool:
            for rows in pool.map(lambda a: self._funding_job(*a), jobs):
                self.con.executemany(UPSERT_FUNDING, rows)
                fetched += len(rows)
        self.con.commit()
        log.info("funding: %d rows over %d perps", fetched, len(jobs))
        return fetched

    def _funding_job(self, symbol, start_ms):
        try:
            return binance.funding(self.http, symbol, start_ms=start_ms)
        except Exception as e:
            self.failures[f"funding:{symbol}"] = str(e)[:200]
            return []

    # --- entrypoint -------------------------------------------------------

    def run(self):
        self.ensure_map()
        if self.args.map_only:
            return 0
        tracked = symbols.tracked(self.con)
        if not tracked:
            log.error("no tracked symbols; check the symbol map")
            return 1

        now_ms = int(time.time() * 1000)
        days = self.args.backfill
        kind = "backfill" if days else "hourly"
        floor_ms = now_ms - (days * 24 if days else self.b["hourly_lookback_hours"]) * HOUR_MS
        started = datetime.now(timezone.utc)
        ingest_id = self.con.execute(
            "INSERT INTO ingest_runs(kind,started_at,symbols) VALUES(?,?,?)",
            (kind, started.isoformat(), len(tracked))).lastrowid
        self.con.commit()

        before = self.counts()
        fetched = self.collect_bars(tracked, floor_ms, now_ms)
        funding = self.collect_funding(tracked, floor_ms, now_ms) if self.b["funding"] else 0
        after = self.counts()

        peak = max(self.http.stats().get("peak_weight", {}).values(), default=0)
        self.con.execute(
            "UPDATE ingest_runs SET finished_at=?,bars_fetched=?,bars_new=?,funding_new=?,"
            "peak_weight=?,failures=? WHERE id=?",
            (datetime.now(timezone.utc).isoformat(), fetched, after[0] - before[0],
             after[1] - before[1], peak, json.dumps(self.failures) if self.failures else None, ingest_id))
        for host, n in self.http.per_host.items():
            add_usage(self.con, host, n)
        self.con.commit()
        self.summary(kind, ingest_id, started, tracked, fetched, before, after)
        return 0

    def counts(self):
        return (self.con.execute("SELECT COUNT(*) c FROM klines_1h").fetchone()["c"],
                self.con.execute("SELECT COUNT(*) c FROM funding_rates").fetchone()["c"])

    def summary(self, kind, ingest_id, started, tracked, fetched, before, after):
        elapsed = (datetime.now(timezone.utc) - started).total_seconds()
        span = self.con.execute("SELECT MIN(open_time) a, MAX(open_time) b FROM klines_1h").fetchone()
        log.info("=" * 58)
        log.info("ingest %d (%s): %d symbols in %.1fs", ingest_id, kind, len(tracked), elapsed)
        log.info("bars fetched=%d  new=%d  total=%d   funding new=%d total=%d",
                 fetched, after[0] - before[0], after[0], after[1] - before[1], after[1])
        if span["a"]:
            iso = lambda ms: datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat()  # noqa: E731
            log.info("coverage: %s .. %s", iso(span["a"]), iso(span["b"]))
        log.info("http: %s", self.http.stats())
        if self.failures:
            log.warning("%d failures: %s", len(self.failures), self.failures)
        log.info("=" * 58)
