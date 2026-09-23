import hashlib
import json
import logging
import time
from pathlib import Path

log = logging.getLogger(__name__)


class Cache:
    """Disk cache of raw API responses: data/cache/<source>/<hash>.json."""

    def __init__(self, root, ttl_seconds, enabled=True):
        self.root, self.ttl, self.enabled = Path(root), ttl_seconds, enabled
        self.hits = self.misses = 0

    def _path(self, source, key):
        h = hashlib.sha256(key.encode()).hexdigest()[:32]
        return self.root / source / f"{h}.json"

    def get(self, source, key):
        if not self.enabled:
            return None
        p = self._path(source, key)
        if not p.exists():
            self.misses += 1
            return None
        try:
            rec = json.loads(p.read_text())
        except (json.JSONDecodeError, OSError):
            self.misses += 1
            return None
        if self.ttl is not None and time.time() - rec["fetched_at"] > self.ttl:
            self.misses += 1
            return None
        self.hits += 1
        return rec["body"]

    def set(self, source, key, body):
        p = self._path(source, key)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps({"key": key, "fetched_at": time.time(), "body": body}))
        tmp.replace(p)

    def stats(self):
        return {"hits": self.hits, "misses": self.misses}
