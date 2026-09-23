import logging

log = logging.getLogger(__name__)
CG = "https://api.coingecko.com/api/v3"
DAY_MS = 86_400_000


def fetch_chart(http, coin_id, window_days):
    days = window_days * 2 + 1
    return http.get_json("coingecko", f"{CG}/coins/{coin_id}/market_chart",
                         {"vs_currency": "usd", "days": days, "interval": "daily"},
                         allow_404=True)


def _bounds(series, window_days):
    if not series:
        return None
    end = series[-1][0]
    return end - window_days * DAY_MS, end - 2 * window_days * DAY_MS


def _at(series, ts):
    """Value of the point nearest to ts, or None if the series does not reach back that far."""
    if not series or ts < series[0][0] - DAY_MS:
        return None
    return min(series, key=lambda p: abs(p[0] - ts))[1]


def window_return(prices, window_days, prior=False):
    b = _bounds(prices, window_days)
    if not b:
        return None
    mid, start = b
    a, z = (start, mid) if prior else (mid, prices[-1][0])
    p0, p1 = _at(prices, a), _at(prices, z)
    return None if not p0 or p1 is None else p1 / p0 - 1.0


def window_sum(series, window_days, prior=False):
    b = _bounds(series, window_days)
    if not b:
        return None
    mid, start = b
    lo, hi = (start, mid) if prior else (mid, series[-1][0] + 1)
    vals = [v for t, v in series if lo < t <= hi and v is not None]
    return sum(vals) if vals else None


def rel_btc(coin_prices, btc_prices, window_days):
    """Contract: ratio where 0.0 == flat vs BTC, i.e. coin return minus BTC return."""
    out = []
    for prior in (False, True):
        c, b = window_return(coin_prices, window_days, prior), window_return(btc_prices, window_days, prior)
        out.append(None if c is None or b is None else c - b)
    return out[0], out[1]
