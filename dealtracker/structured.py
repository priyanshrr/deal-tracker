"""Sources that are already structured: rows come straight from the page, no LLM.

SEBI lists every draft red herring prospectus (DRHP) filed with it. That is
the primary source for the first IPO milestone -- it often appears before any
newspaper writes the story -- and each line is already "company, date, link",
so asking a model to read it would only add cost and a chance of error.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import List, Tuple

from bs4 import BeautifulSoup

from dealtracker.article import Article, parse_date
from dealtracker.records import build_record

log = logging.getLogger("dealtracker.structured")

# "Madhur Iron & Steel India Limited - DRHP ..." -> a new filing.
# "Rayzon Solar Limited - Addendum to the DRHP"  -> an amendment; not a new IPO.
_DRHP = re.compile(r"^\s*(?P<company>.+?)\s*[-–—]\s*DRHP\b", re.I)
_AMENDMENT = re.compile(r"\baddendum\b|\bcorrigendum\b|\bupdated\b", re.I)


def parse_sebi_drhp(html: str, source_name: str, tier: int, max_age_hours: int,
                    now: datetime = None) -> List[Tuple[str, str, str]]:
    """-> [(company, published_iso, url)] for new DRHP filings in the window."""
    now = now or datetime.now(timezone.utc)
    soup = BeautifulSoup(html or "", "lxml")
    out = []
    for tr in soup.select("table tr"):
        cells = tr.find_all("td")
        link = tr.find("a", href=True)
        if len(cells) < 2 or not link:
            continue
        title = cells[1].get_text(" ", strip=True)
        if _AMENDMENT.search(title):
            continue
        m = _DRHP.match(title)
        if not m:
            continue
        published = parse_date(cells[0].get_text(" ", strip=True))
        if published and max_age_hours > 0:
            if datetime.fromisoformat(published) < now - timedelta(hours=max_age_hours):
                continue
        out.append((m.group("company").strip(), published, link["href"]))
    return out


def ingest_sebi_drhp(cfg, fetcher, source, skip_ids) -> Tuple[List[Article], list, str]:
    """-> (articles for seen-tracking, deal records, error)."""
    res = fetcher.get(source["url"])
    if not res.ok:
        return [], [], res.error or "fetch_failed"
    rows = parse_sebi_drhp(res.text, source.name, source.tier,
                           int(cfg.get("fetch.max_article_age_hours", 72)))
    articles, records = [], []
    for company, published, url in rows:
        art = Article(url=url, outlet=source.name, tier=source.tier,
                      title="%s - DRHP" % company, body="", published=published)
        if art.article_id in skip_ids:
            continue
        payload = {
            "event_reported": "%s filed its draft red herring prospectus with SEBI" % company,
            "indian_party": company,          # a SEBI filer is Indian by definition
            "deal_type": "ipo",
            "ipo_milestone": "drhp_filed",
            "company_name": company,
            "company_legal_name": company,
            "sector": "",
            "investors": [],
            "deal_date": (published or "")[:10] or None,
            "confidence": "high",
            "notes": "Primary source: SEBI public issue filings.",
        }
        records.append(build_record(payload, art, cfg.inr_per_usd))
        articles.append(art)
    log.info("[%s] %d new DRHP filings", source.name, len(records))
    return articles, records, ""
