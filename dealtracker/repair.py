"""One-time cleanup of rows written before a rule change.

Code fixes only stop NEW bad rows. Rows already in the sheet stay until
something removes them. A repair is identified by an id in config.yaml, runs
once (its completion is recorded in SQLite), backs the sheet up to a new tab
first, and can be previewed without touching anything:

    python -m dealtracker repair --plan
"""
from __future__ import annotations

import copy
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Tuple

from dealtracker.dedupe import deduplicate
from dealtracker.records import _is_future, parse_amount_usd_mn

log = logging.getLogger("dealtracker.repair")


def plan_repair(cfg, records) -> Dict[str, Any]:
    """Decide what to drop and what to merge. Pure: touches nothing."""
    fx = cfg.inr_per_usd
    drop_ids = set(cfg.get("repair.drop_deal_ids") or [])
    dropped: List[Tuple[Any, str]] = []
    kept = []
    for rec in records:
        # Re-apply today's rules to yesterday's rows.
        derived = parse_amount_usd_mn(rec.amount_as_reported or "", fx)
        if derived is not None:
            rec.amount_usd_mn = derived
        if rec.deal_id in drop_ids:
            dropped.append((rec, "not an Indian deal"))
        elif rec.deal_type == "ipo" and not rec.ipo_milestone:
            dropped.append((rec, "IPO row with no milestone"))
        elif rec.deal_type == "ipo" and rec.ipo_milestone == "listing" and _is_future(rec.deal_date, rec.published):
            dropped.append((rec, "listing dated in the future"))
        else:
            kept.append(rec)

    kept.sort(key=lambda r: (r.published or r.date_added or ""))
    # Snapshot identities BEFORE deduplicating: when a more complete duplicate
    # wins a merge it takes over the other record's deal_id (so the row already
    # in the sheet is the one kept). Reading r.deal_id afterwards would name the
    # SURVIVOR's row for deletion instead of the duplicate's.
    snapshot = [(r.deal_id, copy.copy(r), len(r.sources or [])) for r in kept]
    survivors, _touched, outcome = deduplicate(kept, cfg, existing=[], backend=None)
    survivor_ids = {r.deal_id for r in survivors}
    merged_away = [(orig, into) for oid, orig, _n in snapshot
                   if oid not in survivor_ids
                   for into in [outcome.merged_into.get(oid)]]
    before_sources = {oid: n for oid, _r, n in snapshot}
    grown = [r for r in survivors if len(r.sources or []) > before_sources.get(r.deal_id, 0)]
    remove_ids = [r.deal_id for r, _ in dropped] + [orig.deal_id for orig, _ in merged_away]
    return {"dropped": dropped, "merged_away": merged_away, "survivors": survivors,
            "grown": grown, "remove_ids": remove_ids}


def describe(plan) -> str:
    lines = []
    lines.append("ROWS TO REMOVE (%d)" % len(plan["dropped"]))
    for rec, why in plan["dropped"]:
        lines.append("  - %-32s %-8s %s" % ((rec.company_name or "?")[:32], rec.deal_type, why))
    lines.append("")
    lines.append("DUPLICATE ROWS TO FOLD INTO ANOTHER ROW (%d)" % len(plan["merged_away"]))
    names = {r.deal_id: r.company_name for r in plan["survivors"]}
    for rec, into in plan["merged_away"]:
        lines.append("  - %-32s %-8s from %-18s -> merged into the %s row" % (
            (rec.company_name or "?")[:32], rec.deal_type, rec.source_outlet, names.get(into, into)))
    lines.append("")
    lines.append("ROWS THAT GAIN SOURCES (%d)" % len(plan["grown"]))
    for rec in plan["grown"]:
        lines.append("  + %-32s now %d outlets: %s" % (
            (rec.company_name or "?")[:32], rec.source_count,
            ", ".join(dict.fromkeys(s.get("outlet", "?") for s in rec.sources))))
    return "\n".join(lines)


def maybe_repair(cfg, store) -> Dict[str, Any]:
    repair_id = cfg.get("repair.id")
    if not repair_id:
        return {}
    key = "repair:%s" % repair_id
    if store.meta_get(key):
        return {}
    from dealtracker.sheets import SheetWriter

    records, _vectors = store.load_recent(int(cfg.get("state.retention_days", 14)))
    plan = plan_repair(cfg, records)
    remove_ids = plan["remove_ids"]
    keep_ids = {r.deal_id for r in plan["survivors"]}
    assert not keep_ids & set(remove_ids), "repair would delete a row it means to keep"
    writer = SheetWriter(cfg)
    backup = writer.backup(datetime.now(timezone.utc).strftime("%Y%m%d"))
    log.info("repair %s: sheet backed up to tab %r", repair_id, backup)
    grown = writer.update_sources(plan["grown"]) if plan["grown"] else 0
    removed = writer.delete_deals(remove_ids)
    store.delete_deals(remove_ids)
    store.upsert(plan["survivors"])
    store.meta_set(key, datetime.now(timezone.utc).isoformat())
    log.info("repair %s done: %d rows removed, %d rows updated", repair_id, removed, grown)
    print(describe(plan))
    return {"id": repair_id, "backup_tab": backup, "rows_removed": removed, "rows_updated": grown}
