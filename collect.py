#!/usr/bin/env python3
import argparse
import logging
import sys

from truffle import config
from truffle.ingest import Collector


def parse_args(argv=None):
    p = argparse.ArgumentParser(prog="collect.py", description="Binance hourly OHLCV ingestion")
    p.add_argument("--backfill", type=int, nargs="?", const=0, metavar="DAYS",
                   help="one-time history pull instead of the incremental run "
                        "(default: binance.backfill_days)")
    p.add_argument("--map", dest="map_only", action="store_true",
                   help="rebuild the symbol -> coin_id map and stop")
    p.add_argument("--remap", action="store_true", help="rebuild the symbol map, then collect")
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
    if args.backfill == 0:
        args.backfill = cfg["binance"]["backfill_days"]
    c = Collector(cfg, args)
    if args.remap:
        c.remap()
    return c.run()


if __name__ == "__main__":
    sys.exit(main())
