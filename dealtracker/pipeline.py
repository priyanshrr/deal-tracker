"""The full run: fetch -> near-dup gate -> filter -> extract -> dedupe -> sheet."""
from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from dealtracker.dedupe import calibrate, deduplicate, fingerprint
from dealtracker.embed import get_backend, record_text, shingle_overlap, shingles
from dealtracker.http import build_fetcher
from dealtracker.ingest import ingest_all
from dealtracker.llm import extract_many
from dealtracker.relevance import DropLog, partition
from dealtracker.store import Store

log = logging.getLogger("dealtracker.pipeline")


def _redact(text: str) -> str:
    """Error text goes into a committed, public log -- never let a key through."""
    text = re.sub(r"sk-ant-[A-Za-z0-9_\-]+", "sk-ant-***", text or "")
    text = re.sub(r"ghp_[A-Za-z0-9]+", "ghp_***", text)
    return text.replace("\n", " ")[:240]


class RunSummary(dict):
    def line(self) -> str:
        errors = self.get("errors_by_source") or {}
        return (
            "run=%s fetched=%d thin=%d already_seen=%d near_dup=%d passed_filter=%d "
            "extracted=%d failed=%d none=%d new_deals=%d merged=%d written=%d updated=%d "
            "tokens=%d/%d errors=%s first_extraction_error=%s guard_rejected=%s"
            % (
                self.get("run_id", "?"), self.get("fetched", 0), self.get("thin", 0),
                self.get("already_seen", 0), self.get("near_duplicates", 0),
                self.get("passed_filter", 0), self.get("extracted", 0),
                self.get("extraction_failed", 0), self.get("deal_type_none", 0), self.get("new_deals", 0),
                self.get("merged", 0), self.get("written", 0), self.get("updated", 0),
                self.get("input_tokens", 0), self.get("output_tokens", 0),
                json.dumps(errors, sort_keys=True) if errors else "{}",
                json.dumps(self.get("first_extraction_error") or ""),
                json.dumps(self.get("guard_rejected") or {}, sort_keys=True),
            )
        )


def _near_dup_gate(cfg, store, articles) -> Tuple[List[Any], List[Tuple[Any, str, float]], Dict[str, set]]:
    """Drop near-verbatim reprints before they cost an LLM call."""
    if not bool(cfg.get("dedupe.near_dup_enabled", True)):
        return articles, [], {}
    size = int(cfg.get("dedupe.near_dup_shingle_size", 5))
    threshold = float(cfg.get("dedupe.near_dup_threshold", 0.90))
    hours = int(cfg.get("dedupe.near_dup_lookback_hours", 48))

    known = store.recent_shingles(hours) if store else []
    kept, dropped, computed = [], [], {}
    for art in articles:
        sh = shingles(art.body, size)
        computed[art.article_id] = sh
        best_score, best_url = 0.0, ""
        for _aid, url, other in known:
            score = shingle_overlap(sh, other)
            if score > best_score:
                best_score, best_url = score, url
        if best_score >= threshold:
            dropped.append((art, best_url, best_score))
            log.info("near-duplicate %.2f of %s, dropping before extraction: %s",
                     best_score, best_url, art.url)
            continue
        # Compare within this batch too: eight syndicated copies arriving in one
        # run would otherwise all get through.
        known.append((art.article_id, art.url, sh))
        kept.append(art)
    return kept, dropped, computed


def run(cfg, args) -> Tuple[RunSummary, Dict[str, Any]]:
    run_id = uuid.uuid4().hex[:10]
    summary = RunSummary(run_id=run_id, started_at=datetime.now(timezone.utc).isoformat())
    dry = bool(getattr(args, "dry_run", False))
    calibration = bool(getattr(args, "no_dedup", False))

    store = Store(cfg.path("state.sqlite_path", "data/state.sqlite"))
    backend = get_backend(cfg)
    artifacts: Dict[str, Any] = {}

    # --- 1. ingest -------------------------------------------------
    if getattr(args, "from_cache", None):
        from dealtracker.commands import _load_articles

        articles = _load_articles(args.from_cache)
        reports = []
        summary["fetched"] = len(articles)
        summary["thin"] = 0
        summary["errors_by_source"] = {}
    else:
        sources = cfg.enabled_sources(only=args.source, tiers=args.tier)
        fetcher = build_fetcher(cfg)
        reports = ingest_all(cfg, fetcher, sources, limit=args.limit,
                             skip_ids=store.seen_article_ids())
        articles = [a for r in reports for a in r.articles]
        summary["fetched"] = sum(r.fetched for r in reports)
        summary["thin"] = sum(r.thin for r in reports)
        summary["errors_by_source"] = {
            r.source: r.errors for r in reports if r.errors
        }
        summary["blocked_sources"] = [r.source for r in reports if r.blocked and r.errors]
    artifacts["reports"] = reports

    # Structured sources: rows straight from the page, no model call.
    structured_articles, structured_records = [], []
    if not getattr(args, "from_cache", None):
        from dealtracker.structured import ingest_sebi_drhp

        seen_before = store.seen_article_ids()
        for s in sources:
            if s.get("type") == "sebi_drhp":
                arts, recs, err = ingest_sebi_drhp(cfg, fetcher, s, seen_before)
                structured_articles.extend(arts)
                structured_records.extend(recs)
                if err:
                    summary["errors_by_source"][s.name] = summary["errors_by_source"].get(s.name, 0) + 1
    summary["structured_rows"] = len(structured_records)

    # --- 2. skip articles already extracted in an earlier run ------
    # Most are already gone (ingest skipped them by URL without fetching); this
    # catches the rest, e.g. the same story under two URLs on one site.
    seen = store.seen_article_ids()
    fresh = [a for a in articles if a.article_id not in seen]
    summary["already_seen"] = (
        sum(getattr(r, "already_seen", 0) for r in reports) + len(articles) - len(fresh)
    )

    # --- 3. near-duplicate text gate -------------------------------
    fresh, near_dups, shingle_map = _near_dup_gate(cfg, store, fresh)
    summary["near_duplicates"] = len(near_dups)
    artifacts["near_duplicates"] = near_dups

    # --- 4. relevance filter ---------------------------------------
    drop_log = DropLog(cfg.path("logging.dropped_log", "data/dropped.jsonl"))
    kept, dropped = partition(cfg, fresh, drop_log)
    summary["passed_filter"] = len(kept)
    summary["filtered_out"] = len(dropped)
    candidates = [a for a, _v in kept]

    # --- 5. extraction ---------------------------------------------
    # `_client` is only ever set by the test harness; production leaves it None.
    records, stats = extract_many(cfg, candidates, client=getattr(args, "_client", None))
    records = records + structured_records
    summary["extracted"] = stats.succeeded
    summary["extraction_failed"] = stats.failed
    summary["roundup_titles_skipped"] = stats.skipped_roundup
    summary["deal_type_none"] = stats.deal_none
    summary["input_tokens"] = stats.input_tokens
    summary["output_tokens"] = stats.output_tokens
    summary["first_extraction_error"] = _redact(stats.errors[0]) if stats.errors else ""
    # An article whose extraction FAILED has not been read. Recording it as
    # seen would make every later run skip it, silently losing the deal.
    failed_ids = set(stats.failed_ids)
    seen_now = [a for a in fresh if a.article_id not in failed_ids]
    deals = [r for r in records if r.deal_type != "none"]
    # Deals the code guards turned away (India-only, stage-less IPO, future
    # listing). Recorded with names so an over-strict rule shows up in the log.
    rejected = {}
    for r in records:
        if (r.notes or "").startswith("rejected: "):
            reason = r.notes[len("rejected: "):].split(".", 1)[0]
            rejected.setdefault(reason, []).append(r.company_name or "?")
    summary["guard_rejected"] = {k: {"count": len(v), "examples": v[:6]} for k, v in rejected.items()}
    artifacts["records"] = records

    if not dry and not calibration and not getattr(args, "no_sheet", False):
        try:
            from dealtracker.repair import maybe_repair

            summary["repair"] = maybe_repair(cfg, store)
        except Exception as exc:  # noqa: BLE001 - a failed cleanup must not stop the run
            log.warning("repair failed, will retry next run: %s", exc)
            summary["repair"] = {"error": str(exc)[:200]}

    existing, vectors = store.load_recent(int(cfg.get("dedupe.embedding_lookback_days", 14)))

    # --- 6a. calibration mode: no merging, evidence only -----------
    if calibration:
        entries, new_vectors = calibrate(deals, cfg, existing=existing, backend=backend)
        artifacts["calibration"] = entries
        summary["new_deals"] = len(entries)
        summary["merged"] = 0
        if not dry:
            store.record_articles([(a, shingle_map.get(a.article_id)) for a in seen_now + structured_articles])
            store.upsert([e["record"] for e in entries], vectors=new_vectors,
                         vector_model=backend.model_id,
                         fingerprints={e["record"].deal_id: e["fingerprint"] for e in entries})
        written = 0
        if not dry and not getattr(args, "no_sheet", False):
            from dealtracker.sheets import CalibrationWriter

            written = CalibrationWriter(cfg).append_entries(entries)
        summary["written"] = written
        summary["updated"] = 0
        _finish(cfg, store, summary, dry)
        return summary, artifacts

    # --- 6b. dedupe -------------------------------------------------
    survivors, touched, outcome = deduplicate(
        deals, cfg, existing=existing, vectors=vectors, backend=backend
    )
    summary["new_deals"] = len(survivors)
    summary["merged"] = len(outcome.merged_into)
    summary["fingerprint_merges"] = outcome.fingerprint_merges
    summary["embedding_merges"] = outcome.embedding_merges
    summary["embedding_near_misses"] = outcome.embedding_near_misses
    artifacts["survivors"] = survivors
    artifacts["touched"] = touched
    artifacts["outcome"] = outcome

    # --- 7. sheet ---------------------------------------------------
    written, updated = 0, 0
    row_for: Dict[str, int] = {}
    if not dry and not getattr(args, "no_sheet", False):
        from dealtracker.sheets import SheetWriter

        writer = SheetWriter(cfg)
        row_for = writer.append(survivors)
        written = len(row_for)
        if touched:
            # Rows are found by deal_id in column A, so this still works after
            # rows have been deleted or sorted by hand.
            updated = writer.update_sources(touched)
    summary["written"] = written
    summary["updated"] = updated

    # --- 8. state ---------------------------------------------------
    if not dry:
        store.record_articles([(a, shingle_map.get(a.article_id)) for a in seen_now + structured_articles])
        body_chars = int(cfg.get("dedupe.body_chars_for_embedding", 500))
        new_vectors = {
            r.deal_id: backend.embed(record_text(r, body_chars=body_chars))
            for r in survivors
        }
        store.upsert(survivors, vectors=new_vectors, vector_model=backend.model_id,
                     fingerprints={r.deal_id: fingerprint(r, cfg) or "" for r in survivors})
        store.upsert(touched, vector_model=backend.model_id)

    _finish(cfg, store, summary, dry)
    return summary, artifacts


def _finish(cfg, store, summary, dry: bool) -> None:
    if not dry:
        pruned = store.prune(int(cfg.get("state.retention_days", 14)))
        summary["pruned"] = pruned
        store.log_run(summary["run_id"], dict(summary))
    path = cfg.path("logging.run_log", "data/runs.log")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write("%s %s\n" % (datetime.now(timezone.utc).isoformat(), summary.line()))
    store.close()
