"""The deal record: schema, validation, currency handling."""
from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

DEAL_TYPES = ("funding", "ma", "ipo", "none")
IPO_MILESTONES = ("drhp_filed", "sebi_approval", "price_band", "anchor_book", "listing")
CONFIDENCE = ("high", "medium", "low")

ROUND_STAGES = (
    "Pre-Seed", "Seed", "Pre-Series A", "Series A", "Series B", "Series C",
    "Series D", "Series E", "Series F", "Series G", "Series H", "Bridge",
    "Debt", "Growth", "Pre-IPO",
)

# Indian numbering plus the usual suffixes, in rupees per unit / dollars per unit.
_INR_UNITS = {
    "crore": 1e7, "cr": 1e7, "cr.": 1e7,
    "lakh": 1e5, "lac": 1e5, "lakhs": 1e5,
    "billion": 1e9, "bn": 1e9,
    "million": 1e6, "mn": 1e6, "m": 1e6,
    "thousand": 1e3, "k": 1e3,
}

_AMOUNT_RE = re.compile(
    r"(?P<sym>₹|rs\.?|inr|usd|us\$|\$)?\s*"
    r"(?P<num>\d[\d,]*(?:\.\d+)?)\s*"
    r"(?P<unit>crore|cr\.?|lakhs?|lac|billion|bn|million|mn|m|thousand|k)?"
    r"\s*(?P<sym2>₹|rs\.?|inr|usd|us\$|\$)?",
    re.I,
)

_INR_MARKERS = ("₹", "rs", "inr", "crore", "cr", "lakh", "lac", "rupee")


def parse_amount_usd_mn(text: str, inr_per_usd: float) -> Optional[float]:
    """Parse 'Rs 375 crore' / '$45 Mn' / '₹200 Cr' into USD millions.

    Models are unreliable at arithmetic, so the reported string is the source of
    truth and the conversion happens here, deterministically, at the config rate.
    """
    if not text:
        return None
    low = text.strip().lower()
    m = _AMOUNT_RE.search(low)
    if not m:
        return None
    try:
        num = float(m.group("num").replace(",", ""))
    except ValueError:
        return None
    unit = (m.group("unit") or "").strip(".")
    multiplier = _INR_UNITS.get(unit, 1.0)
    value = num * multiplier

    sym = (m.group("sym") or m.group("sym2") or "").strip().lower()
    is_inr = sym in ("₹", "rs", "rs.", "inr") or any(k in low for k in ("crore", "lakh", "lac", "₹", "rupee"))
    if sym in ("$", "us$", "usd"):
        is_inr = False
    elif not sym:
        is_inr = any(k in low for k in _INR_MARKERS)

    if is_inr:
        if not inr_per_usd:
            return None
        return round(value / inr_per_usd / 1e6, 3)
    return round(value / 1e6, 3)


@dataclass
class DealRecord:
    # --- extracted ---
    deal_type: str = "none"
    ipo_milestone: Optional[str] = None
    company_name: str = ""
    company_legal_name: Optional[str] = None
    sector: str = ""
    amount_usd_mn: Optional[float] = None
    amount_as_reported: Optional[str] = None
    round_stage: Optional[str] = None
    investors: List[str] = field(default_factory=list)
    lead_investor: Optional[str] = None
    acquirer: Optional[str] = None
    target: Optional[str] = None
    valuation_usd_mn: Optional[float] = None
    deal_date: Optional[str] = None
    confidence: str = "low"
    notes: Optional[str] = None

    # --- provenance ---
    source_url: str = ""
    source_outlet: str = ""
    source_tier: int = 0
    published: Optional[str] = None
    article_id: str = ""
    body_excerpt: str = ""

    # --- assigned by the pipeline ---
    deal_id: str = ""
    fingerprint: str = ""
    sources: List[Dict[str, Any]] = field(default_factory=list)
    first_reported_by: str = ""
    source_count: int = 1
    conflicts: List[str] = field(default_factory=list)
    date_added: Optional[str] = None
    amount_recomputed: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @property
    def filled_fields(self) -> int:
        """How complete this record is -- decides which record wins a merge."""
        keys = ("company_name", "company_legal_name", "sector", "amount_usd_mn",
                "amount_as_reported", "round_stage", "lead_investor", "acquirer",
                "target", "valuation_usd_mn", "deal_date", "ipo_milestone", "notes")
        n = sum(1 for k in keys if getattr(self, k) not in (None, "", []))
        return n + (1 if self.investors else 0)


def _clean_str(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() in ("null", "none", "n/a", "na", "unknown", "not disclosed",
                                    "undisclosed", "-"):
        return None
    return text


def _clean_float(value: Any) -> Optional[float]:
    if value in (None, "", "null"):
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if f >= 0 else None


def _normalise_date(value: Any, fallback: Optional[str]) -> Optional[str]:
    text = _clean_str(value)
    if text:
        m = re.search(r"(\d{4})-(\d{2})-(\d{2})", text)
        if m:
            return m.group(0)
        from dateutil import parser as dparser

        try:
            return dparser.parse(text).date().isoformat()
        except (ValueError, OverflowError, TypeError):
            pass
    if fallback:
        try:
            return datetime.fromisoformat(fallback).date().isoformat()
        except ValueError:
            return None
    return None


def build_record(payload: Dict[str, Any], article, inr_per_usd: float) -> DealRecord:
    """Validate and normalise a raw model payload into a DealRecord."""
    deal_type = (_clean_str(payload.get("deal_type")) or "none").lower()
    if deal_type not in DEAL_TYPES:
        deal_type = "none"

    milestone = (_clean_str(payload.get("ipo_milestone")) or "").lower().replace(" ", "_")
    if milestone not in IPO_MILESTONES:
        milestone = None
    if deal_type != "ipo":
        milestone = None

    confidence = (_clean_str(payload.get("confidence")) or "low").lower()
    if confidence not in CONFIDENCE:
        confidence = "low"

    investors = payload.get("investors") or []
    if isinstance(investors, str):
        investors = [investors]
    investors = [i for i in (_clean_str(x) for x in investors) if i]

    reported = _clean_str(payload.get("amount_as_reported"))
    amount = _clean_float(payload.get("amount_usd_mn"))
    recomputed = False
    derived = parse_amount_usd_mn(reported or "", inr_per_usd)
    if derived is not None:
        # Trust the string over the model's arithmetic when they disagree.
        if amount is None or abs(derived - amount) > max(0.05 * max(derived, 0.01), 0.05):
            amount = derived
            recomputed = True

    rec = DealRecord(
        deal_type=deal_type,
        ipo_milestone=milestone,
        company_name=_clean_str(payload.get("company_name")) or "",
        company_legal_name=_clean_str(payload.get("company_legal_name")),
        sector=_clean_str(payload.get("sector")) or "",
        amount_usd_mn=amount,
        amount_as_reported=reported,
        round_stage=_clean_str(payload.get("round_stage")),
        investors=investors,
        lead_investor=_clean_str(payload.get("lead_investor")),
        acquirer=_clean_str(payload.get("acquirer")),
        target=_clean_str(payload.get("target")),
        valuation_usd_mn=_clean_float(payload.get("valuation_usd_mn")),
        deal_date=_normalise_date(payload.get("deal_date"), article.published),
        confidence=confidence,
        notes=_clean_str(payload.get("notes")),
        source_url=article.url,
        source_outlet=article.outlet,
        source_tier=article.tier,
        published=article.published,
        article_id=article.article_id,
        body_excerpt=article.body[:500],
        amount_recomputed=recomputed,
    )
    if rec.deal_type != "ma":
        rec.acquirer = rec.acquirer if rec.deal_type == "ma" else None
        rec.target = rec.target if rec.deal_type == "ma" else None
    rec.sources = [{
        "outlet": article.outlet,
        "url": article.url,
        "published": article.published,
        "tier": article.tier,
    }]
    rec.first_reported_by = article.outlet
    rec.deal_id = hashlib.sha1(
        ("%s|%s" % (article.url, rec.company_name)).encode("utf-8")
    ).hexdigest()[:12]
    rec.date_added = datetime.now(timezone.utc).isoformat()
    return rec
