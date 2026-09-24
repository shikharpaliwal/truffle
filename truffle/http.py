import logging
import random
import threading
import time
from collections import Counter
from urllib.parse import urlparse

import requests

log = logging.getLogger(__name__)


class ApiError(Exception):
    pass


class _HostLimiter:
    """Token-bucket-ish spacing plus a hard pause honoured across threads."""

    def __init__(self, rate_per_min):
        self.min_gap = 60.0 / rate_per_min if rate_per_min else 0.0
        self.next_ok = 0.0
        self.lock = threading.Lock()

    def wait(self):
        with self.lock:
            now = time.monotonic()
            if now < self.next_ok:
                time.sleep(self.next_ok - now)
            self.next_ok = max(time.monotonic(), self.next_ok) + self.min_gap

    def pause(self, seconds):
        with self.lock:
            self.next_ok = max(self.next_ok, time.monotonic() + seconds)


class _WeightLimiter:
    """Binance's model: a call costs weight against a per-minute cap and the response
    reports the running total, so we steer by the server's count rather than our own.
    The counter resets on the wall-clock minute, so that is what we idle to."""

    HEADER = "x-mbx-used-weight-1m"

    def __init__(self, cap, reserve=0.2):
        self.cap, self.ceiling = cap, cap * (1 - reserve)
        self.used = self.peak = self.epoch = 0
        self.lock = threading.Lock()

    def gate(self):
        """Returns the counter epoch this request belongs to."""
        # Held across the sleep on purpose: every other thread must wait out the minute too.
        with self.lock:
            if self.used < self.ceiling:
                return self.epoch
            wait = 61 - time.time() % 60
            log.warning("weight %d/%d used; idling %.0fs to the next minute", self.used, self.cap, wait)
            time.sleep(wait)
            self.used, self.epoch = 0, self.epoch + 1
            return self.epoch

    def observe(self, r, epoch):
        v = r.headers.get(self.HEADER)
        if v is None:
            return
        with self.lock:
            self.peak = max(self.peak, int(v))
            # A reply issued before the counter reset carries a stale total; applying it
            # would re-trip the gate and idle out another whole minute.
            if epoch == self.epoch:
                self.used = max(self.used, int(v))


class HttpClient:
    def __init__(self, cfg, cache):
        h = cfg["http"]
        self.cache, self.timeout = cache, h["timeout"]
        self.max_retries, self.base, self.cap = h["max_retries"], h["backoff_base"], h["backoff_cap"]
        self.limiters = {host: _HostLimiter(v.get("rate_per_min", 60)) for host, v in h["hosts"].items()}
        self.default_limiter = _HostLimiter(30)
        self.weights = {host: _WeightLimiter(v["weight_per_min"])
                        for host, v in h["hosts"].items() if v.get("weight_per_min")}
        self._local = threading.local()      # requests.Session is not thread-safe; give each one its own
        self._stats_lock = threading.Lock()
        self.cg_key = cfg["coingecko_key"]
        self.max_pause = h.get("max_pause_seconds", 120)
        self.disabled = {}  # host -> why; set when a reset is further off than max_pause
        self.calls = 0
        self.per_host = Counter()   # for the monthly credit budget
        self.rate_limit_events = 0
        self.waited = 0.0

    @property
    def session(self):
        s = getattr(self._local, "session", None)
        if s is None:
            s = self._local.session = requests.Session()
            s.headers["User-Agent"] = "truffle-scanner/0.1"
        return s

    def _limiter(self, url):
        return self.limiters.get(urlparse(url).netloc, self.default_limiter)

    def _auth(self, url, headers, params):
        if "coingecko" in urlparse(url).netloc and self.cg_key:
            params = {**(params or {}), "x_cg_demo_api_key": self.cg_key}
        return headers, params

    def get_json(self, source, url, params=None, cache_key=None, headers=None, allow_404=False, cache=True):
        if not cache:
            return self._request(url, params, headers or {}, allow_404)
        key = cache_key or f"{url}?{sorted((params or {}).items())}"
        cached = self.cache.get(source, key)
        if cached is not None:
            return cached
        body = self._request(url, params, headers or {}, allow_404)
        if body is not None:
            self.cache.set(source, key, body)
        return body

    def _request(self, url, params, headers, allow_404):
        host = urlparse(url).netloc
        if host in self.disabled:
            raise ApiError(f"{host} disabled for this run: {self.disabled[host]}")
        headers, params = self._auth(url, headers, params)
        limiter, weight = self._limiter(url), self.weights.get(host)
        last = None
        for attempt in range(self.max_retries + 1):
            epoch = weight.gate() if weight else None
            limiter.wait()
            try:
                r = self.session.get(url, params=params, headers=headers, timeout=self.timeout)
            except requests.RequestException as e:
                last = e
                self._sleep(limiter, attempt, f"{url}: {e}")
                continue
            with self._stats_lock:
                self.calls += 1
                self.per_host[host] += 1
            if weight:
                weight.observe(r, epoch)
            if r.status_code == 404 and allow_404:
                return None
            # 418 is Binance's "you ignored a 429"; both carry Retry-After.
            if r.status_code in (418, 429) or (r.status_code == 403 and "rate limit" in r.text.lower()):
                self.rate_limit_events += 1
                if self._budget_exhausted(r, host):
                    raise ApiError(self.disabled[host])
                self._sleep(limiter, attempt, f"rate limited on {host}", r)
                last = ApiError(f"429 {url}")
                continue
            if r.status_code >= 500:
                last = ApiError(f"{r.status_code} {url}")
                self._sleep(limiter, attempt, f"{r.status_code} from {url}")
                continue
            if r.status_code >= 400:
                raise ApiError(f"{r.status_code} {url}: {r.text[:200]}")
            try:
                return r.json()
            except ValueError:
                raise ApiError(f"non-JSON from {url}")
        raise ApiError(f"exhausted retries for {url}: {last}")

    def _reset_wait(self, r):
        reset = r.headers.get("X-RateLimit-Reset")
        return None if not reset else max(0.0, float(reset) - time.time()) + 1

    def _budget_exhausted(self, r, host):
        """Disable a host whose quota resets further out than we are willing to wait."""
        wait = self._reset_wait(r)
        if wait is None or wait <= self.max_pause:
            return False
        self.disabled[host] = f"quota resets in {wait / 60:.0f} min (> max_pause {self.max_pause}s)"
        log.warning("%s: %s -- skipping it for the rest of this run", host, self.disabled[host])
        return True

    def _sleep(self, limiter, attempt, why, resp=None):
        retry_after = None
        if resp is not None:
            ra = resp.headers.get("Retry-After")
            if ra:
                try:
                    retry_after = float(ra)
                except ValueError:
                    retry_after = None
        delay = retry_after if retry_after is not None else min(self.cap, self.base ** (attempt + 1))
        delay += random.uniform(0, delay * 0.3)  # jitter
        self.waited += delay
        log.warning("%s -> retry %d in %.1fs", why, attempt + 1, delay)
        limiter.pause(delay)

    def stats(self):
        out = {"calls": self.calls, "rate_limit_events": self.rate_limit_events,
               "backoff_seconds": round(self.waited, 1), "disabled_hosts": list(self.disabled),
               **self.cache.stats()}
        peaks = {h: w.peak for h, w in self.weights.items() if w.peak}
        return {**out, "peak_weight": peaks} if peaks else out
