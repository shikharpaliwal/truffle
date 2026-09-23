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


class HttpClient:
    def __init__(self, cfg, cache):
        h = cfg["http"]
        self.cache, self.timeout = cache, h["timeout"]
        self.max_retries, self.base, self.cap = h["max_retries"], h["backoff_base"], h["backoff_cap"]
        self.limiters = {host: _HostLimiter(v.get("rate_per_min", 60)) for host, v in h["hosts"].items()}
        self.default_limiter = _HostLimiter(30)
        self.session = requests.Session()
        self.session.headers["User-Agent"] = "truffle-scanner/0.1"
        self.cg_key, self.gh_token = cfg["coingecko_key"], cfg["github_token"]
        self.max_pause = h.get("max_pause_seconds", 120)
        self.disabled = {}  # host -> why; set when a reset is further off than max_pause
        self.calls = 0
        self.per_host = Counter()   # for the monthly credit budget
        self.rate_limit_events = 0
        self.waited = 0.0

    def _limiter(self, url):
        return self.limiters.get(urlparse(url).netloc, self.default_limiter)

    def _auth(self, url, headers, params):
        host = urlparse(url).netloc
        if "coingecko" in host and self.cg_key:
            params = {**(params or {}), "x_cg_demo_api_key": self.cg_key}
        if host == "api.github.com":
            headers = {**headers, "Accept": "application/vnd.github+json"}
            if self.gh_token:
                headers["Authorization"] = f"Bearer {self.gh_token}"
        return headers, params

    def get_json(self, source, url, params=None, cache_key=None, headers=None, allow_404=False):
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
        limiter = self._limiter(url)
        last = None
        for attempt in range(self.max_retries + 1):
            limiter.wait()
            try:
                r = self.session.get(url, params=params, headers=headers, timeout=self.timeout)
            except requests.RequestException as e:
                last = e
                self._sleep(limiter, attempt, f"{url}: {e}")
                continue
            self.calls += 1
            self.per_host[host] += 1
            if r.status_code == 404 and allow_404:
                return None
            if r.status_code == 429 or (r.status_code == 403 and "rate limit" in r.text.lower()):
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
            self._github_budget(r, limiter)
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

    def _github_budget(self, r, limiter):
        # Pre-emptively idle (or give up) when the remaining budget is nearly gone.
        rem = r.headers.get("X-RateLimit-Remaining")
        if rem is None or int(rem) > 2:
            return
        if self._budget_exhausted(r, urlparse(r.url).netloc):
            return
        wait = self._reset_wait(r) or 1
        log.warning("budget low; pausing %.0fs until reset", wait)
        limiter.pause(wait)

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
        return {"calls": self.calls, "rate_limit_events": self.rate_limit_events,
                "backoff_seconds": round(self.waited, 1), "disabled_hosts": list(self.disabled),
                **self.cache.stats()}
