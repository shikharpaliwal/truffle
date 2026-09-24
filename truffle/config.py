import os
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent


class Config(dict):
    def __getattr__(self, k):
        try:
            return self[k]
        except KeyError:
            raise AttributeError(k)


def load(path=None):
    load_dotenv(ROOT / ".env")
    cfg = Config(yaml.safe_load((Path(path) if path else ROOT / "config.yaml").read_text()))
    cfg["root"] = ROOT
    cfg["db_path"] = ROOT / cfg["db_path"]
    cfg["cache_dir"] = ROOT / cfg["cache_dir"]
    cfg["coingecko_key"] = os.getenv("COINGECKO_API_KEY") or None
    return cfg
