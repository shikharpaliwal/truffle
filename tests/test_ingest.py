import time

import pytest

from truffle import symbols
from truffle.http import _WeightLimiter
from truffle.binance import HOUR_MS, parse_kline
from truffle.db import open_db
from truffle.ingest import UPSERT_BAR, Collector, due, floor_hour

COINS = [
    {"coin_id": "bitcoin", "symbol": "BTC", "name": "Bitcoin", "market_cap": 1e12, "price_usd": 86000.0},
    {"coin_id": "pepe", "symbol": "PEPE", "name": "Pepe", "market_cap": 3e9, "price_usd": 7.5e-6},
    {"coin_id": "cross-2", "symbol": "ONE", "name": "Cross", "market_cap": 2e8, "price_usd": 0.6},
]


def pairs(*specs):
    return [{"symbol": s, "market": m, "baseAsset": b} for s, m, b in specs]


@pytest.fixture
def con(tmp_path):
    return open_db(tmp_path / "t.db")


# --- mapping ----------------------------------------------------------------

def test_symbol_and_price_agreement_maps():
    rows = symbols.resolve(COINS, pairs(("BTCUSDT", "spot", "BTC")), {"BTCUSDT": 85900.0})
    assert rows[0][4:6] == ("bitcoin", "mapped")


def test_price_disagreement_is_flagged_not_guessed():
    """Binance ONE is Harmony; the only universe coin with that ticker is a different project."""
    rows = symbols.resolve(COINS, pairs(("ONEUSDT", "spot", "ONE")), {"ONEUSDT": 0.0095})
    assert rows[0][4] is None and rows[0][5] == "ambiguous"
    assert "cross-2" in rows[0][6]


def test_multiplier_prefix_is_stripped_and_priced_per_unit():
    rows = symbols.resolve(COINS, pairs(("1000PEPEUSDT", "perp", "1000PEPE")), {"1000PEPEUSDT": 0.00745})
    assert rows[0][3] == 1000 and rows[0][4] == "pepe" and rows[0][5] == "mapped"


def test_multiplier_would_be_rejected_without_the_division():
    rows = symbols.resolve(COINS, pairs(("1000PEPEUSDT", "perp", "1000PEPE")), {"1000PEPEUSDT": 7.45})
    assert rows[0][5] == "ambiguous"


def test_unknown_symbol_is_unmapped_not_ambiguous():
    rows = symbols.resolve(COINS, pairs(("ZZZUSDT", "spot", "ZZZ")), {"ZZZUSDT": 1.0})
    assert rows[0][5] == "unmapped"


def test_a_colliding_ticker_is_disambiguated_by_price():
    coins = COINS + [{"coin_id": "one-decoy", "symbol": "ONE", "name": "Decoy",
                      "market_cap": 1e6, "price_usd": 0.0096}]
    rows = symbols.resolve(coins, pairs(("ONEUSDT", "spot", "ONE")), {"ONEUSDT": 0.0095})
    assert rows[0][4] == "one-decoy" and "disambiguated" in rows[0][6]


def test_two_agreeing_candidates_stay_ambiguous():
    coins = COINS + [{"coin_id": "btc-clone", "symbol": "BTC", "name": "Clone",
                      "market_cap": 1e6, "price_usd": 86001.0}]
    rows = symbols.resolve(coins, pairs(("BTCUSDT", "spot", "BTC")), {"BTCUSDT": 86000.0})
    assert rows[0][5] == "ambiguous" and "several" in rows[0][6]


def test_two_pairs_claiming_one_coin_flag_each_other():
    rows = symbols.resolve(COINS, pairs(("PEPEUSDT", "spot", "PEPE"), ("1000PEPEUSDT", "spot", "1000PEPE")),
                           {"PEPEUSDT": 7.5e-6, "1000PEPEUSDT": 0.0075})
    assert {r[5] for r in rows} == {"ambiguous"}


def test_missing_price_is_flagged():
    rows = symbols.resolve(COINS, pairs(("BTCUSDT", "spot", "BTC")), {})
    assert rows[0][5] == "ambiguous" and "verify" in rows[0][6]


def test_store_tracks_only_mapped_rows_and_preserves_manual_ones(con):
    symbols.store(con, symbols.resolve(
        COINS, pairs(("BTCUSDT", "spot", "BTC"), ("ONEUSDT", "spot", "ONE")),
        {"BTCUSDT": 86000.0, "ONEUSDT": 0.0095}))
    assert [(r["symbol"], r["coin_id"]) for r in symbols.tracked(con)] == [("BTCUSDT", "bitcoin")]

    con.execute("UPDATE binance_symbols SET coin_id='harmony', status='mapped', tracked=1, manual=1 "
                "WHERE symbol='ONEUSDT'")
    symbols.store(con, symbols.resolve(COINS, pairs(("ONEUSDT", "spot", "ONE")), {"ONEUSDT": 0.0095}))
    assert len(symbols.tracked(con)) == 2


def test_strip_multiplier_never_eats_the_whole_symbol():
    assert symbols.strip_multiplier("1000", ("1000",)) == ("1000", 1)
    assert symbols.strip_multiplier("1000000MOG", ("1000", "1000000")) == ("MOG", 1000000)
    assert symbols.strip_multiplier("1INCH", ("1000",)) == ("1INCH", 1)


# --- gap detection ----------------------------------------------------------

NOW = 1_700_000_000_000 // HOUR_MS * HOUR_MS      # an exact hour boundary


def test_cold_symbol_starts_at_the_floor():
    assert due(None, NOW - 48 * HOUR_MS, NOW) == NOW - 48 * HOUR_MS


def test_floor_is_snapped_to_the_hour():
    assert due(None, NOW - 48 * HOUR_MS + 1234, NOW) == NOW - 48 * HOUR_MS


def test_next_bar_only_after_it_has_closed():
    last = NOW - 2 * HOUR_MS
    assert due(last, 0, NOW) == NOW - HOUR_MS           # that bar closed
    assert due(NOW - HOUR_MS, 0, NOW) is None           # the current one has not
    assert due(NOW - HOUR_MS, 0, NOW + HOUR_MS - 1) is None


def test_an_arbitrarily_long_gap_resumes_from_the_last_bar_not_the_floor():
    """A missed week must backfill the week, not just the hourly lookback."""
    last = NOW - 300 * HOUR_MS
    assert due(last, NOW - 48 * HOUR_MS, NOW) == last + HOUR_MS


def test_floor_hour():
    assert floor_hour(NOW + 59 * 60_000) == NOW


# --- bar storage ------------------------------------------------------------

def kline(t, close=100.0):
    return [t, "99.0", "101.0", "98.0", str(close), "5.0", t + HOUR_MS - 1, "500.0", 42, "2.5", "250.0", "0"]


def test_parse_kline_maps_the_columns():
    o, h, lo, c, v, qv, n, ct = parse_kline(kline(NOW))[1:]
    assert (o, h, lo, c, v, qv, n, ct) == (99.0, 101.0, 98.0, 100.0, 5.0, 500.0, 42, NOW + HOUR_MS - 1)


def upsert(con, rows):
    con.executemany(UPSERT_BAR, [("BTCUSDT", "spot", *parse_kline(r)[:8]) for r in rows])
    con.commit()
    return con.execute("SELECT COUNT(*) c FROM klines_1h").fetchone()["c"]


def test_reingesting_the_same_bars_does_not_duplicate(con):
    bars = [kline(NOW - i * HOUR_MS) for i in range(10)]
    assert upsert(con, bars) == 10
    assert upsert(con, bars) == 10
    assert upsert(con, bars[3:] + [kline(NOW + HOUR_MS)]) == 11


def test_a_revised_bar_overwrites_in_place(con):
    upsert(con, [kline(NOW, close=100.0)])
    upsert(con, [kline(NOW, close=123.0)])
    row = con.execute("SELECT COUNT(*) c, MAX(close) x FROM klines_1h").fetchone()
    assert (row["c"], row["x"]) == (1, 123.0)


def test_the_same_open_time_on_two_markets_is_two_rows(con):
    con.executemany(UPSERT_BAR, [("BTCUSDT", m, *parse_kline(kline(NOW))[:8]) for m in ("spot", "perp")])
    assert con.execute("SELECT COUNT(*) c FROM klines_1h").fetchone()["c"] == 2


# --- paging over a long gap -------------------------------------------------

class FakeBinance:
    """Serves 1h bars from startTime, MAX_LIMIT at a time, including the open one."""

    def __init__(self, first, last):
        self.first, self.last, self.calls = first, last, 0

    def get_json(self, source, url, params=None, **kw):
        self.calls += 1
        start = max(params.get("startTime", self.first), self.first)
        return [kline(t) for t in range(start, self.last + HOUR_MS, HOUR_MS)][:params["limit"]]


def fetch(http, start, now):
    c = Collector.__new__(Collector)     # fetch_bars only needs .http
    c.http = http
    return Collector.fetch_bars(c, "BTCUSDT", "spot", start, now)


def test_a_long_gap_pages_until_it_catches_up():
    gap = 2500
    http = FakeBinance(NOW - gap * HOUR_MS, NOW)
    bars = fetch(http, NOW - gap * HOUR_MS, NOW)
    assert http.calls == 3 and len(bars) == gap          # the bar opening at NOW is still open
    assert [b[0] for b in bars] == list(range(NOW - gap * HOUR_MS, NOW, HOUR_MS))


def test_the_in_progress_bar_is_never_stored():
    http = FakeBinance(NOW - 5 * HOUR_MS, NOW)
    assert max(b[8] for b in fetch(http, NOW - 5 * HOUR_MS, NOW + 60_000)) < NOW + 60_000


def test_a_symbol_with_no_data_yet_stops_after_one_call():
    http = FakeBinance(NOW + HOUR_MS, NOW)               # nothing in range
    assert fetch(http, NOW - HOUR_MS, NOW) == [] and http.calls == 1


# --- binance weight budget --------------------------------------------------

class Resp:
    def __init__(self, used):
        self.headers = {"x-mbx-used-weight-1m": str(used)}


def test_weight_gate_is_free_below_the_ceiling():
    w = _WeightLimiter(2400)
    w.observe(Resp(1000), w.gate())
    assert w.gate() == 0 and w.used == 1000


def test_a_reply_from_before_the_reset_does_not_re_trip_the_gate(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda s: None)
    w = _WeightLimiter(2400)
    old = w.gate()
    w.observe(Resp(2000), old)                  # past the 80% ceiling
    new = w.gate()                              # idles out the minute and resets
    assert (new, w.used) == (old + 1, 0)
    w.observe(Resp(2001), old)                  # in-flight reply from the old minute
    assert w.used == 0 and w.peak == 2001       # ignored for gating, still counted as a peak
    w.observe(Resp(30), new)
    assert w.used == 30


def test_used_weight_is_monotone_within_a_minute():
    w = _WeightLimiter(2400)
    e = w.gate()
    w.observe(Resp(500), e)
    w.observe(Resp(100), e)                     # replies can land out of order
    assert w.used == 500
