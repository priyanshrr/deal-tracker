"""Command line entry point."""
from __future__ import annotations

import argparse
import logging
import sys
from typing import List, Optional


def _add_source_selection(p: argparse.ArgumentParser) -> None:
    p.add_argument("--source", action="append", default=None,
                   help="restrict to named source(s); overrides `enabled` in sources.yaml")
    p.add_argument("--tier", action="append", type=int, default=None,
                   help="restrict to tier(s)")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="dealtracker",
                                description="Indian private markets deal tracker")
    p.add_argument("--config", default=None)
    p.add_argument("--sources", default=None)
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("discover", help="probe every source for an RSS/Atom feed")
    _add_source_selection(d)
    d.add_argument("--all", action="store_true",
                   help="probe disabled sources too (tier 4 included)")
    d.add_argument("--dry-run", action="store_true", help="report without editing sources.yaml")
    d.add_argument("--concurrency", type=int, default=None,
                   help="parallel sources; lower it when many domains share a host")

    f = sub.add_parser("fetch", help="fetch articles and print them; no LLM calls")
    _add_source_selection(f)
    f.add_argument("--limit", type=int, default=10, help="articles per source")
    f.add_argument("--full", action="store_true", help="print full body text, not a preview")
    f.add_argument("--json", dest="as_json", action="store_true")

    fl = sub.add_parser("filter", help="fetch + relevance filter; show kept and dropped")
    _add_source_selection(fl)
    fl.add_argument("--limit", type=int, default=25)
    fl.add_argument("--show-dropped", action="store_true")

    e = sub.add_parser("extract", help="fetch + filter + LLM extraction")
    _add_source_selection(e)
    e.add_argument("--limit", type=int, default=20, help="max articles sent to the model")
    e.add_argument("--json", dest="as_json", action="store_true")
    e.add_argument("--save", default=None, help="write records to this JSON file")
    e.add_argument("--from-cache", default=None, help="re-extract from a saved articles JSON")

    r = sub.add_parser("run", help="full pipeline: fetch -> filter -> extract -> dedupe -> sheet")
    _add_source_selection(r)
    r.add_argument("--limit", type=int, default=40)
    r.add_argument("--no-dedup", action="store_true",
                   help="calibration mode: one row per record, with fingerprint + top-3 neighbours")
    r.add_argument("--no-sheet", action="store_true", help="skip the Google Sheet write")
    r.add_argument("--dry-run", action="store_true", help="no sheet write, no state write")
    r.add_argument("--from-cache", default=None)

    sub.add_parser("initsheet", help="create/verify the header row on the target sheet")

    m = sub.add_parser("migratesheet",
                       help="reorder an existing sheet's columns to match the current schema")
    m.add_argument("--dry-run", action="store_true",
                   help="show what would change without touching the sheet")
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)-22s %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("urllib3").setLevel(logging.WARNING)

    from dealtracker.config import load_config

    cfg = load_config(args.config, args.sources)

    if args.cmd == "discover":
        from dealtracker.discover import apply_and_report, discover_all
        from dealtracker.http import build_fetcher

        fetcher = build_fetcher(cfg)
        if args.all:
            targets = [s for s in cfg.sources
                       if not args.tier or s.tier in args.tier]
            if args.source:
                names = {o.lower() for o in args.source}
                targets = [s for s in targets if s.name.lower() in names]
        else:
            targets = cfg.enabled_sources(only=args.source, tiers=args.tier)
        probes = discover_all(cfg, fetcher, targets, workers=args.concurrency)
        print(apply_and_report(cfg, probes, write=not args.dry_run))
        if not args.dry_run:
            print("sources.yaml updated with resolved feeds.")
        return 0

    from dealtracker import commands

    return commands.dispatch(cfg, args)
