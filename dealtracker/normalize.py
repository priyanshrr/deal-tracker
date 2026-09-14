"""Name normalisation.

Company and investor names are the least reliable fields in the record -- the
same firm appears as "Peak XV", "Peak XV Partners" and "PeakXV" across outlets --
so every comparison goes through here first.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Iterable, List

# Dropped from investor/acquirer names. "Capital", "Ventures", "Partners",
# "LLP" and "Fund" are required by the brief; the rest are the same idea.
FIRM_SUFFIXES = {
    "capital", "capitals", "ventures", "venture", "partners", "partner",
    "llp", "llc", "fund", "funds", "advisors", "advisers", "management",
    "asset", "investments", "investment", "holdings", "holding", "group",
    "pvt", "private", "limited", "ltd", "inc", "incorporated", "corp",
    "corporation", "co", "company", "plc", "gmbh", "sa", "nv", "bv",
    "associates", "equity", "growth", "india", "global", "international",
    "technologies", "technology", "tech", "labs", "solutions", "services",
    "enterprises", "industries", "systems", "trust", "office",
}

# Only stripped from company names, never from investor names.
COMPANY_ONLY_SUFFIXES = {"the"}

_PUNCT = re.compile(r"[^\w\s]", re.UNICODE)
_WS = re.compile(r"\s+")


def _base(name: str) -> str:
    if not name:
        return ""
    text = unicodedata.normalize("NFKD", str(name))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.lower()
    text = _PUNCT.sub(" ", text)
    return _WS.sub(" ", text).strip()


def normalise_firm(name: str) -> str:
    """'Peak XV Partners' and 'Peak XV' both -> 'peak xv'."""
    tokens = [t for t in _base(name).split() if t not in FIRM_SUFFIXES]
    if not tokens:
        # The name was nothing but suffixes ("Capital Fund"); keep it as-is
        # rather than collapsing every such name to the empty string.
        tokens = _base(name).split()
    return " ".join(tokens)


def normalise_company(name: str) -> str:
    """'Kiranakart Technologies Private Limited' -> 'kiranakart'."""
    tokens = [
        t for t in _base(name).split()
        if t not in FIRM_SUFFIXES and t not in COMPANY_ONLY_SUFFIXES
    ]
    if not tokens:
        tokens = _base(name).split()
    return " ".join(tokens)


def top_investors(investors: Iterable[str], n: int = 3) -> List[str]:
    """Normalised, de-duplicated, sorted -- so outlet ordering cannot matter."""
    seen = []
    for inv in investors or []:
        norm = normalise_firm(inv)
        if norm and norm not in seen:
            seen.append(norm)
    return sorted(seen)[:n]
