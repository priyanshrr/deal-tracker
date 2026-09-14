"""Build-time feed discovery.

For each source: try the hand-written hints, then the homepage's
<link rel="alternate"> declarations, then the conventional paths. Prefer RSS.
Anything that resolves to a parseable feed with entries becomes type: rss;
everything else falls back to type: html and gets scraped from its listing page.
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import List, Optional, Tuple
from urllib.parse import urljoin, urlparse

import feedparser
from bs4 import BeautifulSoup

log = logging.getLogger("dealtracker.discover")

CONVENTIONAL_PATHS = [
    "/feed/",
    "/feed",
    "/rss",
    "/rss/",
    "/feed.xml",
    "/rss.xml",
    "/atom.xml",
    "/index.xml",
]

FEED_MIMES = ("application/rss+xml", "application/atom+xml", "application/xml", "text/xml")


@dataclass
class Probe:
    source_name: str
    resolved_feed: Optional[str] = None
    entry_count: int = 0
    tried: List[Tuple[str, str]] = field(default_factory=list)  # (url, outcome)

    @property
    def kind(self) -> str:
        return "rss" if self.resolved_feed else "html"


def _looks_like_feed(text: str) -> Tuple[bool, int]:
    """A candidate counts only if it parses AND carries entries with links."""
    head = text.lstrip()[:400].lower()
    if head.startswith("<!doctype html") or head.startswith("<html"):
        return False, 0
    parsed = feedparser.parse(text)
    entries = [e for e in getattr(parsed, "entries", []) if e.get("link")]
    if not entries:
        return False, 0
    if not getattr(parsed, "version", ""):
        return False, 0
    return True, len(entries)


def _homepage_alternates(fetcher, url: str) -> List[str]:
    res = fetcher.get(url)
    if not res.ok:
        return []
    try:
        soup = BeautifulSoup(res.text, "lxml")
    except Exception:  # noqa: BLE001
        return []
    out = []
    for link in soup.find_all("link", rel=lambda v: v and "alternate" in " ".join(v).lower()):
        mime = (link.get("type") or "").lower()
        href = link.get("href")
        if href and any(m in mime for m in FEED_MIMES):
            out.append(urljoin(res.final_url or url, href))
    # Comment feeds are noise; the main feed is what we want.
    return [u for u in out if "comments" not in u.lower()]


def probe_source(fetcher, source) -> Probe:
    probe = Probe(source_name=source.name)
    origin = "{0.scheme}://{0.netloc}".format(urlparse(source["url"]))

    candidates: List[str] = []
    seen = set()

    def add(u: Optional[str]) -> None:
        if u and u not in seen:
            seen.add(u)
            candidates.append(u)

    # 1. A feed already pinned in sources.yaml wins outright.
    add(source.get("feed"))
    # 2. Hand-written hints (site-specific paths that no generic probe would find).
    for hint in source.get("feed_hints") or []:
        add(hint)
    # 3. What the homepage itself declares.
    for alt in _homepage_alternates(fetcher, source["url"]):
        add(alt)
    # 4. Conventional paths, on both the section URL and the origin.
    for path in CONVENTIONAL_PATHS:
        add(urljoin(origin + "/", path.lstrip("/")))
        if source["url"].rstrip("/") != origin:
            add(source["url"].rstrip("/") + path)

    for cand in candidates:
        res = fetcher.get(cand)
        if not res.ok:
            probe.tried.append((cand, res.error or "fetch_failed"))
            continue
        is_feed, count = _looks_like_feed(res.text)
        if is_feed:
            probe.tried.append((cand, "FEED (%d entries)" % count))
            probe.resolved_feed = res.final_url or cand
            probe.entry_count = count
            return probe
        probe.tried.append((cand, "not-a-feed"))
    return probe


def discover_all(cfg, fetcher, sources, workers: Optional[int] = None) -> List[Probe]:
    workers = int(workers or cfg.get("fetch.max_domain_concurrency", 8))
    workers = max(1, min(workers, max(1, len(sources))))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(lambda s: probe_source(fetcher, s), sources))


def apply_and_report(cfg, probes, write: bool = True) -> str:
    feeds, scrapers = [], []
    for p in probes:
        cfg.set_source_field(p.source_name, "type", p.kind)
        cfg.set_source_field(p.source_name, "feed", p.resolved_feed)
        (feeds if p.resolved_feed else scrapers).append(p)
    if write:
        cfg.save_sources()

    lines = []
    lines.append("=" * 78)
    lines.append("FEED DISCOVERY  --  %d sources probed" % len(probes))
    lines.append("=" * 78)
    lines.append("")
    lines.append("RESOLVED TO A FEED (%d):" % len(feeds))
    for p in sorted(feeds, key=lambda x: x.source_name.lower()):
        lines.append("  %-22s %-4d entries  %s" % (p.source_name, p.entry_count, p.resolved_feed))
    lines.append("")
    lines.append("NEEDS AN HTML SCRAPER (%d):" % len(scrapers))
    if not scrapers:
        lines.append("  (none)")
    for p in sorted(scrapers, key=lambda x: x.source_name.lower()):
        lines.append("  %s" % p.source_name)
        for url, outcome in p.tried[:6]:
            lines.append("      tried %-58s -> %s" % (url[:58], outcome))
        if len(p.tried) > 6:
            lines.append("      ... %d more candidates tried" % (len(p.tried) - 6))
    lines.append("")
    return "\n".join(lines)
