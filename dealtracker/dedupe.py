"""Deduplication: deterministic fingerprints, embedding fallback, merge.

Never keys on company name. Outlets variously use the brand ("Zepto") or the
legal entity ("Kiranakart Technologies Private Limited"), so the name is the
least reliable field in the record and is only ever used after normalisation,
and only for IPOs where the milestone carries the discriminating power.
"""
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Sequence, Tuple

from dealtracker.embed import cosine, record_text
from dealtracker.normalize import normalise_company, normalise_firm, top_investors

log = logging.getLogger("dealtracker.dedupe")

CONFIDENCE_RANK = {"high": 3, "medium": 2, "low": 1}


@dataclass
class Neighbour:
    deal_id: str
    company: str
    score: float
    deal_type: str


@dataclass
class DedupeOutcome:
    merged_into: Dict[str, str] = field(default_factory=dict)   # deal_id -> surviving deal_id
    neighbours: Dict[str, List[Neighbour]] = field(default_factory=dict)
    fingerprint_merges: int = 0
    embedding_merges: int = 0
    embedding_near_misses: int = 0


def _as_date(value: Optional[str]) -> Optional[date]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value)[:10]).date()
    except ValueError:
        return None


def _within_days(a: Optional[str], b: Optional[str], days: int) -> bool:
    da, db = _as_date(a), _as_date(b)
    if da is None or db is None:
        # A missing date must not silently license a merge.
        return False
    return abs((da - db).days) <= days


def _hash(parts: Sequence[Any]) -> str:
    raw = "|".join("" if p is None else str(p) for p in parts)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def fingerprint(record, cfg) -> Optional[str]:
    """The date-independent part of the key. None => this record cannot be
    fingerprinted and must fall through to stage 2."""
    decimals = int(cfg.get("dedupe.amount_round_decimals", 1))
    top_n = int(cfg.get("dedupe.fingerprint_top_investors", 3))

    if record.deal_type == "funding":
        # Blocking key is amount + stage. Investors are compared separately, by
        # OVERLAP, in _funding_compatible.
        #
        # The brief specifies the top 3 normalised investor names as part of the
        # key. Taken literally that makes stage 1 brittle in the common case:
        # Entrackr names one investor, Inc42 names three, the sorted top-3 sets
        # differ, the key differs, and the same round becomes two rows -- which
        # breaks "one row per deal". Requiring a non-empty intersection instead
        # is still fully deterministic, and it only ever merges rounds that share
        # a named investor.
        if record.amount_usd_mn is None:
            return None
        if not record.round_stage and not record.investors and not record.company_name:
            return None
        return "f:" + _hash([
            "funding",
            round(float(record.amount_usd_mn), decimals),
            (record.round_stage or "").strip().lower(),
        ])

    if record.deal_type == "ma":
        # Acquirer + target is highly reliable: outlets rarely disagree on who
        # bought whom. Amount is frequently undisclosed so it is NOT in the key.
        acquirer = normalise_firm(record.acquirer or "")
        target = normalise_company(record.target or record.company_name or "")
        if not acquirer or not target:
            return None
        return "m:" + _hash(["ma", acquirer, target])

    if record.deal_type == "ipo":
        # An IPO is a sequence, not a point event. DRHP, SEBI approval, price
        # band, anchor book and listing are five legitimate rows for one company,
        # so the milestone is part of the key. Merging on company alone would
        # collapse the whole timeline into a single row.
        company = normalise_company(record.company_name or "")
        if not company or not record.ipo_milestone:
            return None
        return "i:" + _hash(["ipo", company, record.ipo_milestone])

    return None


def _date_window(record, cfg) -> int:
    return int({
        "funding": cfg.get("dedupe.funding_date_window_days", 7),
        "ma": cfg.get("dedupe.ma_date_window_days", 7),
        "ipo": cfg.get("dedupe.ipo_date_window_days", 14),
    }.get(record.deal_type, 7))


def _company_agrees(a, b) -> bool:
    """A guard, never a key.

    Company name is the least reliable field, so it is only ever used to BLOCK a
    merge that has no other corroboration -- it can never cause one on its own.
    """
    ca, cb = normalise_company(a.company_name or ""), normalise_company(b.company_name or "")
    if not ca or not cb:
        return False
    return ca == cb or ca in cb or cb in ca


def _funding_compatible(a, b, cfg) -> bool:
    top_n = int(cfg.get("dedupe.fingerprint_top_investors", 3))
    ia = set(top_investors(a.investors, top_n))
    ib = set(top_investors(b.investors, top_n))
    if ia and ib:
        # Both named investors: they must share at least one.
        return bool(ia & ib)
    # One side named none. Amount + stage + date alone would merge two unrelated
    # $5M seed rounds in the same week, so require the company to agree as well.
    return _company_agrees(a, b)


def fingerprint_match(a, b, cfg) -> bool:
    if a.deal_type != b.deal_type:
        return False
    fa, fb = fingerprint(a, cfg), fingerprint(b, cfg)
    if not fa or fa != fb:
        return False
    if not _within_days(a.deal_date, b.deal_date, _date_window(a, cfg)):
        return False
    if a.deal_type == "funding":
        return _funding_compatible(a, b, cfg)
    return True


# --- merging --------------------------------------------------------------

def _better_base(a, b):
    """Most non-null fields wins; ties break on confidence, then on tier."""
    key = lambda r: (r.filled_fields, CONFIDENCE_RANK.get(r.confidence, 0), -r.source_tier)
    return a if key(a) >= key(b) else b


def _conflicts(base, other) -> List[str]:
    out = []
    ba, oa = base.amount_usd_mn, other.amount_usd_mn
    if ba is not None and oa is not None and max(ba, oa) > 0:
        drift = abs(ba - oa) / max(ba, oa) * 100.0
        if drift > _CONFLICT_PCT[0]:
            out.append("amount: %s says %s, %s says %s (%.0f%% apart)" % (
                base.source_outlet, ba, other.source_outlet, oa, drift))
    if base.lead_investor and other.lead_investor:
        if normalise_firm(base.lead_investor) != normalise_firm(other.lead_investor):
            out.append("lead_investor: %s says %s, %s says %s" % (
                base.source_outlet, base.lead_investor,
                other.source_outlet, other.lead_investor))
    if base.round_stage and other.round_stage:
        if base.round_stage.strip().lower() != other.round_stage.strip().lower():
            out.append("round_stage: %s says %s, %s says %s" % (
                base.source_outlet, base.round_stage,
                other.source_outlet, other.round_stage))
    if base.valuation_usd_mn is not None and other.valuation_usd_mn is not None:
        bv, ov = base.valuation_usd_mn, other.valuation_usd_mn
        if max(bv, ov) > 0 and abs(bv - ov) / max(bv, ov) * 100.0 > _CONFLICT_PCT[0]:
            out.append("valuation: %s says %s, %s says %s" % (
                base.source_outlet, bv, other.source_outlet, ov))
    return out


_CONFLICT_PCT = [10.0]


def merge(base, other, cfg):
    """Fold `other` into `base`, returning the surviving record."""
    _CONFLICT_PCT[0] = float(cfg.get("dedupe.amount_conflict_pct", 10.0))
    winner = _better_base(base, other)
    loser = other if winner is base else base

    conflicts = list(dict.fromkeys(winner.conflicts + loser.conflicts + _conflicts(winner, loser)))

    # Fill gaps in the winner from the loser -- a merge should never lose a fact.
    for attr in ("company_legal_name", "sector", "amount_usd_mn", "amount_as_reported",
                 "round_stage", "lead_investor", "acquirer", "target",
                 "valuation_usd_mn", "deal_date", "ipo_milestone", "notes"):
        if getattr(winner, attr) in (None, "") and getattr(loser, attr) not in (None, ""):
            setattr(winner, attr, getattr(loser, attr))
    for inv in loser.investors:
        if normalise_firm(inv) not in {normalise_firm(i) for i in winner.investors}:
            winner.investors.append(inv)

    seen_urls = {s.get("url") for s in winner.sources}
    for src in loser.sources:
        if src.get("url") not in seen_urls:
            winner.sources.append(src)
            seen_urls.add(src.get("url"))

    dated = [s for s in winner.sources if s.get("published")]
    if dated:
        winner.first_reported_by = min(dated, key=lambda s: s["published"])["outlet"]
    winner.source_count = len({s.get("outlet") for s in winner.sources if s.get("outlet")})
    winner.conflicts = conflicts
    if CONFIDENCE_RANK.get(loser.confidence, 0) > CONFIDENCE_RANK.get(winner.confidence, 0):
        winner.confidence = loser.confidence
    return winner


# --- the two-stage pass ---------------------------------------------------

def deduplicate(records, cfg, existing=None, vectors=None, backend=None):
    """Collapse `records` against each other and against `existing` history.

    `vectors` maps deal_id -> np.ndarray for the existing records.
    Returns (surviving_new_records, updated_existing_records, outcome).
    """
    existing = list(existing or [])
    vectors = dict(vectors or {})
    outcome = DedupeOutcome()

    threshold = float(cfg.get("dedupe.embedding_threshold", 0.93))
    merge_on_embedding = bool(cfg.get("dedupe.embedding_merge_enabled", False))
    body_chars = int(cfg.get("dedupe.body_chars_for_embedding", 500))

    survivors: List = []
    touched_existing: Dict[str, Any] = {}

    def vector_for(rec):
        if backend is None:
            return None
        if rec.deal_id in vectors:
            return vectors[rec.deal_id]
        vec = backend.embed(record_text(rec, body_chars=body_chars))
        vectors[rec.deal_id] = vec
        return vec

    for rec in records:
        if rec.deal_type == "none":
            continue
        pool = survivors + existing
        target = None

        # --- Stage 1: deterministic fingerprint ---
        for cand in pool:
            if fingerprint_match(rec, cand, cfg):
                target = cand
                outcome.fingerprint_merges += 1
                break

        # --- Stage 2: embedding fallback ---
        if target is None and backend is not None:
            vec = vector_for(rec)
            scored: List[Tuple[float, Any]] = []
            for cand in pool:
                if cand.deal_type != rec.deal_type:
                    continue
                if not _within_days(rec.deal_date, cand.deal_date, _date_window(rec, cfg)):
                    continue
                score = cosine(vec, vector_for(cand))
                scored.append((score, cand))
            scored.sort(key=lambda s: -s[0])
            outcome.neighbours[rec.deal_id] = [
                Neighbour(deal_id=c.deal_id, company=c.company_name, score=round(s, 4),
                          deal_type=c.deal_type)
                for s, c in scored[:3]
            ]
            if scored and scored[0][0] >= threshold:
                if merge_on_embedding:
                    target = scored[0][1]
                    outcome.embedding_merges += 1
                else:
                    outcome.embedding_near_misses += 1
                    log.info("embedding match %.3f >= %.3f but merging is disabled: %s ~ %s",
                             scored[0][0], threshold, rec.company_name,
                             scored[0][1].company_name)

        if target is None:
            survivors.append(rec)
            continue

        merged = merge(target, rec, cfg)
        outcome.merged_into[rec.deal_id] = merged.deal_id
        if any(merged is s for s in survivors):
            pass
        elif any(merged is e for e in existing):
            touched_existing[merged.deal_id] = merged
        else:
            # The incoming record won the base contest against a stored record.
            for i, e in enumerate(existing):
                if e.deal_id == target.deal_id:
                    existing[i] = merged
                    break
            touched_existing[merged.deal_id] = merged
        vectors.pop(rec.deal_id, None)

    return survivors, list(touched_existing.values()), outcome


def calibrate(records, cfg, existing=None, backend=None):
    """Calibration mode: no merging, just the evidence.

    Every record keeps its own row, carrying the fingerprint it would have
    matched on and its three nearest neighbours by cosine. Run this for a week
    and pick the stage-2 threshold from the score distribution of your own
    sources, rather than trusting a default that was tuned on someone else's.
    """
    existing = list(existing or [])
    body_chars = int(cfg.get("dedupe.body_chars_for_embedding", 500))
    rows = []
    vectors = {}

    def vec(rec):
        if rec.deal_id not in vectors:
            vectors[rec.deal_id] = backend.embed(record_text(rec, body_chars=body_chars))
        return vectors[rec.deal_id]

    pool = list(records) + existing
    for rec in records:
        if rec.deal_type == "none":
            continue
        fp = fingerprint(rec, cfg)
        neighbours: List[Neighbour] = []
        fp_twin = None
        for cand in pool:
            if cand is rec or cand.deal_id == rec.deal_id:
                continue
            if fp_twin is None and fingerprint_match(rec, cand, cfg):
                fp_twin = cand.deal_id
        if backend is not None:
            scored = []
            for cand in pool:
                if cand is rec or cand.deal_id == rec.deal_id:
                    continue
                scored.append((cosine(vec(rec), vec(cand)), cand))
            scored.sort(key=lambda s: -s[0])
            neighbours = [
                Neighbour(deal_id=c.deal_id, company=c.company_name,
                          score=round(s, 4), deal_type=c.deal_type)
                for s, c in scored[:3]
            ]
        rec.fingerprint = fp or ""
        rows.append({
            "record": rec,
            "fingerprint": fp or "",
            "fingerprint_twin": fp_twin or "",
            "neighbours": neighbours,
        })
    return rows, vectors
