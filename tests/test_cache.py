import json
import time

import pytest

from truffle.cache import Cache


@pytest.fixture
def cache(tmp_path):
    return Cache(tmp_path, ttl_seconds=60)


def test_roundtrip(cache):
    cache.set("coingecko", "k1", {"a": 1})
    assert cache.get("coingecko", "k1") == {"a": 1}
    assert cache.stats() == {"hits": 1, "misses": 0}


def test_miss_returns_none(cache):
    assert cache.get("coingecko", "nope") is None
    assert cache.stats()["misses"] == 1


def test_sources_are_namespaced(cache, tmp_path):
    cache.set("coingecko", "k", 1)
    cache.set("github", "k", 2)
    assert cache.get("coingecko", "k") == 1 and cache.get("github", "k") == 2
    assert {p.name for p in tmp_path.iterdir()} == {"coingecko", "github"}


def test_ttl_expiry(tmp_path):
    c = Cache(tmp_path, ttl_seconds=0)
    c.set("s", "k", {"v": 1})
    time.sleep(0.01)
    assert c.get("s", "k") is None


def test_disabled_cache_never_reads_but_still_writes(tmp_path):
    c = Cache(tmp_path, ttl_seconds=60, enabled=False)
    c.set("s", "k", {"v": 1})
    assert c.get("s", "k") is None          # --refresh bypasses reads
    assert Cache(tmp_path, 60).get("s", "k") == {"v": 1}   # but the file is refreshed on disk


def test_records_fetched_at(cache, tmp_path):
    cache.set("s", "k", [1, 2])
    rec = json.loads(next((tmp_path / "s").iterdir()).read_text())
    assert rec["key"] == "k" and rec["body"] == [1, 2]
    assert abs(rec["fetched_at"] - time.time()) < 5


def test_corrupt_file_is_a_miss(cache, tmp_path):
    cache.set("s", "k", {"v": 1})
    next((tmp_path / "s").iterdir()).write_text("{not json")
    assert cache.get("s", "k") is None


def test_falsy_bodies_are_cache_hits(cache):
    cache.set("s", "empty", [])
    assert cache.get("s", "empty") == []
    assert cache.stats()["hits"] == 1
