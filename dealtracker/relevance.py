"""Cheap keyword gate ahead of the model.

Runs on title + first paragraph only. Every drop is logged with the text that
was judged, so the filter can be audited for being too tight.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

log = logging.getLogger("dealtracker.relevance")

CATEGORIES = ("funding", "ma", "ipo")


@dataclass
class Verdict:
    passed: bool
    categories: List[str] = field(default_factory=list)
    matched: List[str] = field(default_factory=list)
    judged_text: str = ""


class RelevanceFilter:
    def __init__(self, cfg):
        self.min_hits = int(cfg.get("relevance.min_hits", 1))
        self.patterns: Dict[str, List] = {}
        for cat in CATEGORIES:
            terms = cfg.get("relevance.%s" % cat, []) or []
            self.patterns[cat] = [(t, self._compile(t)) for t in terms]

    @staticmethod
    def _compile(term: str):
        """Literal phrase by default; raw regex when the term starts with `re:`.

        Regex terms exist because the interesting patterns are discontinuous --
        "Navam Capital leads Rs 22 Cr round in DigitalPaani" is a funding round,
        but no fixed phrase catches "leads ... round".
        """
        term = term.strip()
        if term.lower().startswith("re:"):
            return re.compile(term[3:], re.I)
        # Word-boundary match so "ipo" does not fire inside "ipod", and phrases
        # tolerate any run of whitespace/punctuation between words.
        parts = [re.escape(w) for w in term.lower().split()]
        return re.compile(r"\b" + r"[\s\-]+".join(parts) + r"\b", re.I)

    def judge(self, title: str, first_para: str) -> Verdict:
        text = ("%s. %s" % (title or "", first_para or "")).strip()
        cats, matched = [], []
        for cat, terms in self.patterns.items():
            for term, rx in terms:
                if rx.search(text):
                    matched.append(term)
                    if cat not in cats:
                        cats.append(cat)
        passed = len(matched) >= self.min_hits
        return Verdict(passed=passed, categories=cats, matched=matched, judged_text=text[:400])


class DropLog:
    """Append-only record of everything the filter rejected."""

    def __init__(self, path: Optional[Path]):
        self.path = Path(path) if path else None
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)

    def record(self, article, verdict: Verdict, reason: str = "no_keyword_match") -> None:
        if not self.path:
            return
        row = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "reason": reason,
            "outlet": article.outlet,
            "tier": article.tier,
            "url": article.url,
            "title": article.title,
            "published": article.published,
            "body_chars": article.body_chars,
            "judged_text": verdict.judged_text,
        }
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def partition(cfg, articles, drop_log: Optional[DropLog] = None):
    """-> (kept, dropped) where each element is (article, verdict)."""
    rf = RelevanceFilter(cfg)
    kept, dropped = [], []
    for art in articles:
        verdict = rf.judge(art.title, art.first_para)
        if verdict.passed:
            kept.append((art, verdict))
        else:
            dropped.append((art, verdict))
            if drop_log:
                drop_log.record(art, verdict)
    log.info("relevance: %d kept, %d dropped", len(kept), len(dropped))
    return kept, dropped
