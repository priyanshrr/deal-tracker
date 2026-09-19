"""Subcommand implementations."""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import List

from dealtracker.http import build_fetcher
from dealtracker.ingest import ingest_all

log = logging.getLogger("dealtracker.cmd")

RULE = "=" * 78


def _select(cfg, args):
    sources = cfg.enabled_sources(only=args.source, tiers=args.tier)
    if not sources:
        raise SystemExit("no sources selected (check --source/--tier and `enabled` in sources.yaml)")
    return sources


def _ingest_summary(reports) -> str:
    lines = [RULE, "%-22s %-6s %-7s %-8s %-6s %s" %
             ("SOURCE", "KIND", "LISTED", "FETCHED", "THIN", "ERRORS"), RULE]
    for r in sorted(reports, key=lambda x: (x.tier, x.source)):
        note = "  [blocked]" if r.blocked and r.errors else ""
        lines.append("%-22s %-6s %-7d %-8d %-6d %d%s" %
                     (r.source, r.kind, r.listed, r.fetched, r.thin, r.errors, note))
    total = sum(len(r.articles) for r in reports)
    lines.append(RULE)
    lines.append("usable articles: %d" % total)
    return "\n".join(lines)


def cmd_fetch(cfg, args) -> int:
    fetcher = build_fetcher(cfg)
    reports = ingest_all(cfg, fetcher, _select(cfg, args), limit=args.limit)
    articles = [a for r in reports for a in r.articles]

    if args.as_json:
        print(json.dumps([a.to_dict() for a in articles], indent=2, ensure_ascii=False))
        return 0

    for a in articles:
        print("\n" + RULE)
        print("%s  [tier %d]  %s" % (a.outlet, a.tier, a.published or "no date"))
        print(a.title)
        print(a.url)
        print("-" * 78)
        print(a.body if args.full else (a.body[:600] + ("..." if len(a.body) > 600 else "")))
    print("\n" + _ingest_summary(reports))
    return 0


def cmd_filter(cfg, args) -> int:
    from dealtracker.relevance import DropLog, partition

    fetcher = build_fetcher(cfg)
    reports = ingest_all(cfg, fetcher, _select(cfg, args), limit=args.limit)
    articles = [a for r in reports for a in r.articles]
    drop_log = DropLog(cfg.path("logging.dropped_log", "data/dropped.jsonl"))
    kept, dropped = partition(cfg, articles, drop_log)

    print("\n" + RULE)
    print("KEPT (%d)" % len(kept))
    print(RULE)
    for art, v in kept:
        print("%-16s %-10s %s" % (art.outlet, ",".join(v.categories), art.title[:80]))
        print("%-16s %-10s   matched: %s" % ("", "", ", ".join(v.matched[:6])))

    print("\n" + RULE)
    print("DROPPED (%d)   -- logged to %s" % (len(dropped), cfg.get("logging.dropped_log")))
    print(RULE)
    for art, v in dropped:
        print("%-16s %s" % (art.outlet, art.title[:88]))
        if args.show_dropped:
            print("%-16s   %s" % ("", v.judged_text[:200].replace("\n", " ")))

    total = len(kept) + len(dropped)
    rate = (100.0 * len(kept) / total) if total else 0.0
    print("\n" + _ingest_summary(reports))
    print("relevance: %d/%d kept (%.0f%%)" % (len(kept), total, rate))
    return 0


ARTICLE_CACHE = "data/articles_cache.json"


def _load_articles(path):
    from dealtracker.article import Article

    rows = json.loads(Path(path).read_text(encoding="utf-8"))
    out = []
    for row in rows:
        row.pop("article_id", None)
        out.append(Article(**row))
    return out


def _save_articles(path, articles) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        json.dumps([a.to_dict() for a in articles], indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def _have_api_key() -> bool:
    import os

    return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))


def gather_candidates(cfg, args, limit):
    """fetch -> relevance filter -> the articles that would reach the model."""
    from dealtracker.relevance import DropLog, partition

    fetcher = build_fetcher(cfg)
    reports = ingest_all(cfg, fetcher, _select(cfg, args), limit=args.limit)
    articles = [a for r in reports for a in r.articles]
    drop_log = DropLog(cfg.path("logging.dropped_log", "data/dropped.jsonl"))
    kept, _dropped = partition(cfg, articles, drop_log)
    return [a for a, _v in kept][:limit], reports


def _print_record(idx, art, rec) -> None:
    print("\n" + RULE)
    print("[%02d] %s  |  tier %d  |  %s" % (idx, art.outlet, art.tier, art.published or "no date"))
    print("     %s" % art.title)
    print("     %s" % art.url)
    print("-" * 78)
    payload = {
        "event_reported": rec.event_reported,
        "deal_type": rec.deal_type,
        "ipo_milestone": rec.ipo_milestone,
        "company_name": rec.company_name,
        "company_legal_name": rec.company_legal_name,
        "sector": rec.sector,
        "amount_usd_mn": rec.amount_usd_mn,
        "amount_as_reported": rec.amount_as_reported,
        "round_stage": rec.round_stage,
        "investors": rec.investors,
        "lead_investor": rec.lead_investor,
        "acquirer": rec.acquirer,
        "target": rec.target,
        "valuation_usd_mn": rec.valuation_usd_mn,
        "deal_date": rec.deal_date,
        "confidence": rec.confidence,
        "notes": rec.notes,
    }
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    if rec.amount_recomputed:
        print("     [fx] amount_usd_mn recomputed in code from amount_as_reported")


def cmd_extract(cfg, args) -> int:
    from dealtracker.llm import extract_many

    if args.from_cache:
        articles = _load_articles(args.from_cache)[: args.limit]
        print("loaded %d cached articles from %s" % (len(articles), args.from_cache))
        reports = []
    else:
        articles, reports = gather_candidates(cfg, args, args.limit)
        _save_articles(ARTICLE_CACHE, articles)
        print("cached %d candidate articles to %s" % (len(articles), ARTICLE_CACHE))

    if not _have_api_key():
        print("\n" + RULE)
        print("ANTHROPIC_API_KEY is not set -- no extraction was attempted.")
        print("The %d articles above are cached, so re-running costs no refetch:" % len(articles))
        print("  export ANTHROPIC_API_KEY=sk-ant-...")
        print("  python -m dealtracker extract --from-cache %s --limit %d" % (ARTICLE_CACHE, args.limit))
        print(RULE)
        return 2

    records, stats = extract_many(cfg, articles)
    by_url = {a.url: a for a in articles}
    if args.as_json:
        print(json.dumps([r.to_dict() for r in records], indent=2, ensure_ascii=False))
    else:
        for i, rec in enumerate(records, 1):
            _print_record(i, by_url.get(rec.source_url, articles[0]), rec)

    if args.save:
        Path(args.save).parent.mkdir(parents=True, exist_ok=True)
        Path(args.save).write_text(
            json.dumps([r.to_dict() for r in records], indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        print("\nsaved %d records to %s" % (len(records), args.save))

    print("\n" + RULE)
    if reports:
        print(_ingest_summary(reports))
    kinds = {}
    for r in records:
        kinds[r.deal_type] = kinds.get(r.deal_type, 0) + 1
    print("extraction: %d attempted, %d ok, %d failed, %d roundup titles skipped pre-call"
          % (stats.attempted, stats.succeeded, stats.failed, stats.skipped_roundup))
    print("deal_type:  %s" % ", ".join("%s=%d" % kv for kv in sorted(kinds.items())))
    print("tokens:     %d in / %d out" % (stats.input_tokens, stats.output_tokens))
    for err in stats.errors[:5]:
        print("error:      %s" % err)
    return 0


def _print_run_report(summary, artifacts, calibration: bool) -> None:
    reports = artifacts.get("reports") or []
    if reports:
        print("\n" + _ingest_summary(reports))
    print("\n" + RULE)
    print("RUN %s" % summary.get("run_id"))
    print(RULE)
    order = [
        ("fetched", "articles fetched"),
        ("thin", "thin/paywalled, skipped pre-model"),
        ("already_seen", "seen in an earlier run, skipped"),
        ("near_duplicates", "near-verbatim reprints dropped"),
        ("passed_filter", "passed the relevance filter"),
        ("roundup_titles_skipped", "roundup titles skipped pre-call"),
        ("extracted", "extracted by the model"),
        ("extraction_failed", "extraction errors"),
        ("deal_type_none", "returned deal_type=none"),
        ("new_deals", "new deals"),
        ("merged", "merged into an existing deal"),
        ("fingerprint_merges", "  ...by fingerprint"),
        ("embedding_merges", "  ...by embedding"),
        ("embedding_near_misses", "  embedding matches NOT merged (stage 2 off)"),
        ("written", "rows written to the sheet"),
        ("updated", "existing rows updated"),
    ]
    for key, label in order:
        if key in summary:
            print("  %-44s %s" % (label, summary[key]))
    if summary.get("errors_by_source"):
        print("  errors by source:")
        for src, n in sorted(summary["errors_by_source"].items()):
            blocked = " [blocked]" if src in (summary.get("blocked_sources") or []) else ""
            print("      %-24s %d%s" % (src, n, blocked))
    print("  tokens: %d in / %d out" % (summary.get("input_tokens", 0),
                                        summary.get("output_tokens", 0)))
    if summary.get("extraction_failed") and not summary.get("extracted"):
        print("\n" + "!" * 78)
        print("EXTRACTION FAILED: every call to the model errored, so nothing was written.")
        print("These articles were NOT marked as seen; the next run will retry them.")
        print("first error: %s" % summary.get("first_extraction_error"))
        print("!" * 78)

    for art, url, score in (artifacts.get("near_duplicates") or [])[:10]:
        print("  near-dup %.2f  %s\n              ~ %s" % (score, art.url, url))

    if calibration:
        entries = artifacts.get("calibration") or []
        print("\n" + RULE)
        print("CALIBRATION -- every record kept, nothing merged")
        print(RULE)
        print("%-26s %-9s %-8s %-8s %s" % ("COMPANY", "TYPE", "FP?", "FP-TWIN", "TOP-3 NEIGHBOURS (cosine)"))
        for e in entries:
            rec = e["record"]
            nn = "  ".join("%s=%.3f" % (n.company[:16] or "?", n.score) for n in e["neighbours"])
            print("%-26s %-9s %-8s %-8s %s" % (
                (rec.company_name or "?")[:26], rec.deal_type,
                "yes" if e["fingerprint"] else "no",
                (e["fingerprint_twin"] or "-")[:8], nn))
        scores = [n.score for e in entries for n in e["neighbours"]]
        if scores:
            scores.sort(reverse=True)
            print("\n  neighbour score distribution: max=%.3f p90=%.3f median=%.3f min=%.3f"
                  % (scores[0], scores[int(len(scores) * 0.1)],
                     scores[len(scores) // 2], scores[-1]))
            print("  pick dedupe.embedding_threshold above the highest score seen between")
            print("  two records you judge to be DIFFERENT deals.")
    else:
        for rec in (artifacts.get("survivors") or []):
            flag = " [%d sources]" % rec.source_count if rec.source_count > 1 else ""
            print("  + %-9s %-28s %-14s %s%s" % (
                rec.deal_type, (rec.company_name or "?")[:28],
                rec.amount_as_reported or "undisclosed",
                rec.first_reported_by, flag))
            for c in rec.conflicts:
                print("      conflict: %s" % c)
        for rec in (artifacts.get("touched") or []):
            print("  ~ updated %-28s now %d sources" % (rec.company_name[:28], rec.source_count))


def cmd_run(cfg, args) -> int:
    from dealtracker.pipeline import run

    if not args.from_cache and not _have_api_key():
        print("ANTHROPIC_API_KEY is not set -- extraction cannot run.")
        return 2
    summary, artifacts = run(cfg, args)
    _print_run_report(summary, artifacts, bool(args.no_dedup))
    print("\nlog: %s" % cfg.get("logging.run_log"))
    return 0


def cmd_initsheet(cfg, args) -> int:
    from dealtracker.sheets import COLUMNS, SheetWriter

    writer = SheetWriter(cfg)
    header = writer.ensure_header()
    print("worksheet '%s' ready with %d columns" % (writer.worksheet_name, len(header)))
    print("  " + ", ".join(COLUMNS))
    print("\nread_flag is yours: the pipeline never writes to it.")
    return 0


def _parse_plain_sources(cell: str):
    """Turn 'Entrackr: https://a | Inc42: https://b' back into outlet/url pairs."""
    out = []
    for chunk in (cell or "").split("|"):
        chunk = chunk.strip()
        if not chunk:
            continue
        outlet, _, url = chunk.partition(": ")
        url = url.strip()
        if url.startswith("http"):
            out.append({"outlet": outlet.strip(), "url": url})
        elif chunk.startswith("http"):
            out.append({"outlet": "source", "url": chunk})
    return out


def cmd_repair(cfg, args) -> int:
    from dealtracker.repair import describe, plan_repair
    from dealtracker.store import Store

    store = Store(cfg.path("state.sqlite_path", "data/state.sqlite"))
    rid = cfg.get("repair.id")
    if not rid:
        print("no repair configured")
        return 0
    if store.meta_get("repair:%s" % rid):
        print("repair %s already applied" % rid)
        return 0
    records, _ = store.load_recent(int(cfg.get("state.retention_days", 14)))
    plan = plan_repair(cfg, records)
    print("REPAIR %s -- preview, nothing is changed\n" % rid)
    print(describe(plan))
    print("\nThe next GitHub run applies this once, after copying the sheet to a backup tab.")
    return 0


def cmd_forget(cfg, args) -> int:
    """Un-see articles recorded since a timestamp, so the next run retries them."""
    import sqlite3

    db = cfg.path("state.sqlite_path", "data/state.sqlite")
    con = sqlite3.connect(str(db))
    n = con.execute("SELECT COUNT(*) FROM articles WHERE seen_at >= ?", (args.since,)).fetchone()[0]
    if args.dry_run:
        print("would forget %d articles recorded since %s" % (n, args.since))
        return 0
    con.execute("DELETE FROM articles WHERE seen_at >= ?", (args.since,))
    con.commit()
    con.close()
    print("forgot %d articles recorded since %s; the next run will retry them" % (n, args.since))
    return 0


def cmd_migratesheet(cfg, args) -> int:
    """One-time: move existing rows into the current column order.

    Reordering the header alone would leave every existing value under the
    wrong heading, so the data moves with it. `read_flag` travels with its row,
    so your own marks are preserved.
    """
    import datetime
    from types import SimpleNamespace

    from dealtracker.sheets import COLUMNS, SheetWriter, sources_rich_cell

    writer = SheetWriter(cfg)
    ws = writer.worksheet
    values = ws.get_all_values()
    if not values:
        print("sheet is empty - just run `initsheet`")
        return 0

    old_header, data = values[0], values[1:]
    data = [r for r in data if any(c.strip() for c in r)]
    if old_header[: len(COLUMNS)] == COLUMNS:
        print("already in the current column order; nothing to do (%d data rows)" % len(data))
        return 0

    moved = [c for c in COLUMNS if c in old_header and COLUMNS.index(c) != old_header.index(c)]
    print("%d data rows found" % len(data))
    print("columns that move: %s" % ", ".join(moved) or "(none)")
    dropped = [c for c in old_header if c and c not in COLUMNS]
    if dropped:
        print("WARNING - these columns are not in the current schema and would be lost: %s"
              % ", ".join(dropped))
        print("aborting; tell me about them first")
        return 1

    index = {name: i for i, name in enumerate(old_header)}
    width = len(old_header)
    new_rows = []
    for row in data:
        row = list(row) + [""] * (width - len(row))
        new_rows.append([row[index[c]] if c in index else "" for c in COLUMNS])

    if args.dry_run:
        print("\n-- first row, before and after --")
        before = list(data[0]) + [""] * (width - len(data[0]))
        for name, val in list(zip(old_header, before))[:6]:
            print("   before  %-18s %s" % (name, val[:48]))
        print()
        for name, val in list(zip(COLUMNS, new_rows[0]))[:6]:
            print("   after   %-18s %s" % (name, val[:48]))
        print("\ndry run - nothing was written")
        return 0

    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = Path("data/sheet_backup_%s.json" % stamp)
    backup.parent.mkdir(parents=True, exist_ok=True)
    backup.write_text(json.dumps({"header": old_header, "rows": data},
                                 indent=2, ensure_ascii=False), encoding="utf-8")
    print("backed up the current sheet to %s" % backup)

    ws.update(range_name="A1", values=[COLUMNS] + new_rows,
              value_input_option="USER_ENTERED")
    print("rewrote %d rows in the new column order" % len(new_rows))

    # Re-apply clickable links to the rows that were already there.
    col = COLUMNS.index("sources")
    requests = []
    for i, row in enumerate(new_rows):
        srcs = _parse_plain_sources(row[col])
        if not srcs:
            continue
        rec = SimpleNamespace(sources=srcs)
        requests.append({
            "updateCells": {
                "range": {"sheetId": ws.id,
                          "startRowIndex": i + 1, "endRowIndex": i + 2,
                          "startColumnIndex": col, "endColumnIndex": col + 1},
                "rows": [{"values": [sources_rich_cell(rec)]}],
                "fields": "userEnteredValue,textFormatRuns",
            }
        })
    if requests:
        try:
            ws.spreadsheet.batch_update({"requests": requests})
            print("made source links clickable on %d existing rows" % len(requests))
        except Exception as exc:  # noqa: BLE001
            print("could not add clickable links (%s); the plain URLs are still there" % exc)
    print("\ndone. Your read_flag marks travelled with their rows.")
    return 0


DISPATCH = {
    "migratesheet": cmd_migratesheet,
    "forget": cmd_forget,
    "repair": cmd_repair,
    "fetch": cmd_fetch,
    "filter": cmd_filter,
    "extract": cmd_extract,
    "run": cmd_run,
    "initsheet": cmd_initsheet,
}


def dispatch(cfg, args) -> int:
    fn = DISPATCH.get(args.cmd)
    if fn is None:
        raise SystemExit("command not implemented yet: %s" % args.cmd)
    return fn(cfg, args)
