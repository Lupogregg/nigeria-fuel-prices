"""Command line: python -m fuelapi {ingest,news,community,build,update,parse,extract}"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="fuelapi", description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=ROOT / "data")
    parser.add_argument("--out-dir", type=Path, default=ROOT / "public")
    sub = parser.add_subparsers(dest="cmd", required=True)

    def add_ingest_args(p: argparse.ArgumentParser) -> None:
        p.add_argument("--backfill", action="store_true",
                       help="also fetch every archived workbook back to 2017 (one-off)")
        p.add_argument("--wayback-only", action="store_true",
                       help="skip NBS and use the Internet Archive mirror only")
        p.add_argument("--limit", type=int, default=None, help="fetch at most N new files")

    add_ingest_args(sub.add_parser("ingest", help="download new NBS workbooks"))
    p_news = sub.add_parser("news", help="poll news feeds and store new fuel-price headlines")
    p_news.add_argument("--days", type=int, default=7,
                        help="how far back the search feeds look (default 7; use 30 once to seed)")
    sub.add_parser("community", help="pull accepted reports from the Telegram bot "
                                     "(needs COMMUNITY_EXPORT_URL and COMMUNITY_EXPORT_TOKEN)")
    sub.add_parser("build", help="rebuild public/v1 JSON from everything under data/")
    add_ingest_args(sub.add_parser("update", help="ingest, news, community, then build"))
    p_parse = sub.add_parser("parse", help="show what the parser extracts from one workbook")
    p_parse.add_argument("file", type=Path)
    p_extract = sub.add_parser("extract", help="show what the news extractor reads from a headline")
    p_extract.add_argument("headline")

    args = parser.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):  # headlines carry the naira sign; Windows consoles default to cp1252
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    if args.cmd in ("ingest", "update"):
        from . import sources
        summary = sources.ingest(args.data_dir, backfill=args.backfill,
                                 wayback_only=args.wayback_only, limit=args.limit)
        print(f"ingest: {len(summary['new'])} new, {len(summary['failed'])} failed")
        for f in summary["failed"]:
            print(f"  failed {f['key']}: {f['error']}")

    if args.cmd in ("news", "update"):
        from . import news
        s = news.fetch(args.data_dir, days=getattr(args, "days", 7))
        print(f"news: {s['feeds_ok']} feeds read, {len(s['feeds_failed'])} failed, "
              f"{s['headlines_seen']} headlines seen, {s['new_items']} new price headlines")
        for f in s["feeds_failed"]:
            print(f"  failed {f}")

    if args.cmd in ("community", "update"):
        from . import community
        url, token = os.environ.get("COMMUNITY_EXPORT_URL"), os.environ.get("COMMUNITY_EXPORT_TOKEN")
        if not url or not token:
            print("community: skipped (COMMUNITY_EXPORT_URL / COMMUNITY_EXPORT_TOKEN not set)")
        else:
            try:
                s = community.sync(args.data_dir, url, token)
                print(f"community: {s['accepted']} new accepted reports "
                      f"({s['skipped_not_accepted']} rejected by the bot, {s['skipped_invalid']} invalid)")
            except Exception as exc:  # noqa: BLE001 - the bot being down must not block the NBS build
                print(f"community: sync failed ({type(exc).__name__}: {exc}); building from what is stored")

    if args.cmd in ("build", "update"):
        from . import build
        report = build.build(args.data_dir, args.out_dir)
        print(json.dumps(report, indent=2))
        if not report["fuels"]:
            print("build: no data - run `python -m fuelapi ingest --backfill` first", file=sys.stderr)
            return 1

    if args.cmd == "parse":
        from . import nbs_parse
        wb = nbs_parse.parse_workbook(args.file.read_bytes(), args.file.name)
        for s in wb.sheets:
            print(f"sheet {s.sheet!r}: fuel={s.fuel} report_period={s.report_period} "
                  f"periods={len(s.periods)} observations={len(s.observations)}"
                  + (f" SKIPPED: {s.skipped}" if s.skipped else ""))
            for w in s.warnings:
                print(f"  warning: {w}")
            for p in s.periods[-3:]:
                print(f"  {p}: national={s.national.get(p)}")
    if args.cmd == "extract":
        from . import news_extract
        print(json.dumps(news_extract.extract(args.headline).to_dict(), indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
