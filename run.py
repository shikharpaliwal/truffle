#!/usr/bin/env python3
import argparse
import logging
import sys

from truffle import config
from truffle.pipeline import Pipeline


def parse_args(argv=None):
    p = argparse.ArgumentParser(prog="run.py", description="Truffle fundamental-momentum scanner")
    p.add_argument("--limit", type=int, help="universe size override (default: config universe_size)")
    p.add_argument("--refresh", "--no-cache", dest="refresh", action="store_true", help="bypass the disk cache")
    p.add_argument("--skip-metadata", action="store_true", help="never refetch static metadata")
    p.add_argument("--ignore-budget", action="store_true", help="run even if the monthly API credit budget would be exceeded")
    p.add_argument("--dry-run", action="store_true", help="build the universe, print a call projection, write nothing")
    p.add_argument("--date", help="run date YYYY-MM-DD (default: today UTC)")
    p.add_argument("--config", help="path to config.yaml")
    p.add_argument("-v", "--verbose", action="store_true")
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)-22s %(message)s", datefmt="%H:%M:%S")
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    cfg = config.load(args.config)
    run_id = Pipeline(cfg, args).run()
    return 0 if run_id is not None or args.dry_run else 1


if __name__ == "__main__":
    sys.exit(main())
