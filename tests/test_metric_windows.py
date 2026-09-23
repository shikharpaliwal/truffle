import pytest

from truffle.metrics.exchanges import exchange_count, reputable_share
from truffle.metrics.market import rel_btc, window_return, window_sum

DAY = 86_400_000


def series(values, end_ms=100 * DAY):
    return [[end_ms - (len(values) - 1 - i) * DAY, v] for i, v in enumerate(values)]


def test_window_return_current_and_prior():
    s = series([100.0] * 31 + [110.0] * 30)   # flat, then a step up
    assert window_return(s, 30) == pytest.approx(0.10)
    assert window_return(s, 30, prior=True) == pytest.approx(0.0)


def test_window_return_needs_enough_history():
    assert window_return(series([1.0] * 10), 30, prior=True) is None


def test_window_sum_splits_the_two_windows():
    # 61 daily points: the boundary point belongs to the prior window (lo exclusive,
    # hi inclusive), so the two windows hold 30 points each with no overlap.
    s = series([1.0] * 31 + [2.0] * 30)
    assert window_sum(s, 30) == pytest.approx(60.0)
    assert window_sum(s, 30, prior=True) == pytest.approx(30.0)


def test_rel_btc_is_zero_when_coin_tracks_btc():
    coin = series([100.0] * 31 + [120.0])
    btc = series([50.0] * 31 + [60.0])
    cur, _ = rel_btc(coin, btc, 30)
    assert cur == pytest.approx(0.0)


def test_rel_btc_positive_when_coin_beats_btc():
    coin = series([100.0] * 31 + [150.0])
    btc = series([100.0] * 31 + [110.0])
    assert rel_btc(coin, btc, 30)[0] == pytest.approx(0.40)


def test_rel_btc_null_without_btc():
    assert rel_btc(series([1.0] * 61), [], 30) == (None, None)


TRUST = {"gdax": 10, "binance": 10, "shadyex": 1, "noscore": None}


def tick(ex, vol, ts=None):
    return {"market": {"identifier": ex}, "converted_volume": {"usd": vol}, "trust_score": ts}


def test_reputable_share_excludes_low_trust():
    t = [tick("gdax", 100), tick("shadyex", 300)]
    assert reputable_share(t, TRUST) == pytest.approx(0.25)


def test_reputable_share_none_without_volume():
    assert reputable_share([tick("gdax", 0)], TRUST) is None


def test_ticker_level_green_counts():
    assert reputable_share([tick("noscore", 100, ts="green")], TRUST) == pytest.approx(1.0)


def test_exchange_count_is_distinct_green_exchanges():
    t = [tick("gdax", 5), tick("gdax", 5), tick("binance", 5), tick("shadyex", 5)]
    assert exchange_count(t, TRUST) == 2
    assert exchange_count(t, TRUST, green_only=False) == 3


def test_slug_map_prefers_the_highest_tvl_protocol_per_gecko_id():
    from truffle.metrics.tvl import slug_map

    class FakeHttp:
        def get_json(self, *a, **k):
            return [{"gecko_id": "aave", "slug": "aave-v2", "tvl": 100.0},
                    {"gecko_id": "aave", "slug": "aave-v3", "tvl": 900.0},
                    {"gecko_id": "ethereum", "slug": "ethereum-foundation", "tvl": None},
                    {"gecko_id": None, "slug": "orphan", "tvl": 5.0},
                    {"gecko_id": "x", "slug": None, "tvl": 5.0}]

    m = slug_map(FakeHttp())
    assert m == {"aave": "aave-v3", "ethereum": "ethereum-foundation"}
