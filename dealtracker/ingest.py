"""Turn a source into Articles: RSS where a feed exists, HTML scraping where not."""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Set, Tuple
from urllib.parse import urldefrag, urljoin, urlparse

import feedparser
from bs4 import BeautifulSoup

from dealtracker.article import (
    Article,
    compile_patterns,
    extract_body,
    extract_published,
    extract_title,
    looks_truncated,
    parse_date,
)

log = logging.getLogger("dealtracker.ingest")

# Fallback only. The real list lives in sources.yaml under
# defaults.exclude_url_patterns, extended per source.
NON_ARTICLE_HINTS = (
    "/tag/", "/tags/", "/category/", "/author/", "/topic/", "/page/",
    "/about", "/contact", "/privacy", "/terms", "/subscribe", "/login",
    "/newsletter", "/advertise", "/videos/", "/photos/", "/podcast",
)


@dataclass
class SourceReport:
    source: str
    tier: int
    kind: str = ""
    listed: int = 0            # entries/links the source offered
    fetched: int = 0           # article pages successfully retrieved
    thin: int = 0              # under min_body_chars -> skipped before the model
    errors: int = 0
    blocked: bool = False
    error_detail: str = ""
    articles: List[Article] = field(default_factory=list)


def _is_plausible_article(url: str, origin_netloc: str, excludes=NON_ARTICLE_HINTS) -> bool:
    parts = urlparse(url)
    if parts.scheme not in ("http", "https"):
        return False
    if parts.netloc and parts.netloc.lower().lstrip("www.") != origin_netloc.lower().lstrip("www."):
        return False
    path = parts.path.rstrip("/")
    if not path or path.count("/") < 1:
        return False
    low = path.lower()
    if any(h in low + "/" for h in excludes):
        return False
    slug = path.rsplit("/", 1)[-1]
    return ("-" in slug and len(slug) > 15) or path.count("/") >= 3


def _within_age(iso: Optional[str], max_hours: int) -> bool:
    if not iso or max_hours <= 0:
        return True
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        return True
    return dt >= datetime.now(timezone.utc) - timedelta(hours=max_hours)


def _entry_date(entry) -> Optional[str]:
    for key in ("published", "updated", "created"):
        if entry.get(key):
            iso = parse_date(entry.get(key))
            if iso:
                return iso
    for key in ("published_parsed", "updated_parsed"):
        st = entry.get(key)
        if st:
            try:
                return datetime(*st[:6], tzinfo=timezone.utc).isoformat()
            except (TypeError, ValueError):
                pass
    return None


def _feed_text(entry) -> str:
    """Richest text the feed itself carries (content:encoded beats summary)."""
    best = ""
    content = entry.get("content") or []
    if isinstance(content, list):
        for item in content:
            value = (item or {}).get("value", "")
            text = BeautifulSoup(value or "", "lxml").get_text("\n", strip=True)
            if len(text) > len(best):
                best = text
    for key in ("summary", "description"):
        text = BeautifulSoup(entry.get(key, "") or "", "lxml").get_text("\n", strip=True)
        if len(text) > len(best):
            best = text
    return best


def _feed_candidates(fetcher, source, limit: int, max_age_hours: int) -> List[Tuple[str, str, Optional[str], str]]:
    """-> [(url, title, published_iso, feed_text)]"""
    res = fetcher.get(source["feed"])
    if not res.ok:
        raise RuntimeError(res.error or "feed_fetch_failed")
    parsed = feedparser.parse(res.text)
    out = []
    for entry in parsed.entries:
        link = (entry.get("link") or "").strip()
        if not link:
            continue
        published = _entry_date(entry)
        if not _within_age(published, max_age_hours):
            continue
        out.append((urldefrag(link)[0], entry.get("title", "").strip(), published, _feed_text(entry)))
        if len(out) >= limit:
            break
    return out


def _html_candidates(fetcher, source, limit: int) -> List[Tuple[str, str, Optional[str], str]]:
    res = fetcher.get(source["url"])
    if not res.ok:
        raise RuntimeError(res.error or "listing_fetch_failed")
    soup = BeautifulSoup(res.text, "lxml")
    base = res.final_url or source["url"]
    origin = urlparse(base).netloc
    selector = source.selector("listing_links") or "a[href]"
    excludes = tuple(source.get("exclude_url_patterns") or NON_ARTICLE_HINTS)

    seen: Set[str] = set()
    out = []
    nodes = []
    for sel in [s.strip() for s in selector.split(",") if s.strip()]:
        try:
            nodes.extend(soup.select(sel))
        except Exception:  # noqa: BLE001
            continue
    for node in nodes:
        href = node.get("href")
        if not href:
            continue
        url = urldefrag(urljoin(base, href))[0]
        if url in seen or not _is_plausible_article(url, origin, excludes):
            continue
        seen.add(url)
        out.append((url, node.get_text(" ", strip=True), None, ""))
        if len(out) >= limit:
            break
    return out


def ingest_source(cfg, fetcher, source, limit: Optional[int] = None) -> SourceReport:
    limit = limit or int(cfg.get("fetch.max_items_per_source", 40))
    max_age = int(cfg.get("fetch.max_article_age_hours", 72))
    min_chars = int(cfg.get("extraction.min_body_chars", 200))
    rep = SourceReport(source=source.name, tier=source.tier, kind=source["type"])
    rep.blocked = "blocked" in source.flags

    try:
        if source["type"] == "rss" and source.get("feed"):
            candidates = _feed_candidates(fetcher, source, limit, max_age)
        else:
            candidates = _html_candidates(fetcher, source, limit)
    except Exception as exc:  # noqa: BLE001 - one dead source never fails the run
        rep.errors += 1
        rep.error_detail = str(exc)
        log.warning("[%s] listing failed: %s", source.name, exc)
        return rep

    rep.listed = len(candidates)
    body_sel = source.selector("article_body")
    title_sel = source.selector("article_title")
    boilerplate = compile_patterns(cfg.get("extraction.boilerplate_patterns"))
    truncation = compile_patterns(cfg.get("extraction.truncation_markers"))

    for url, title, published, feed_text in candidates:
        res = fetcher.get(url)
        flags = list(source.flags)
        if res.ok:
            body = extract_body(res.text, body_sel, boilerplate)
            page_title = extract_title(res.text, title_sel)
            page_date = extract_published(res.text)
        else:
            # The page is blocked or gone. Some outlets (YourStory) publish the
            # whole article in the feed, so that is a real body, not a fallback
            # guess -- but if the feed is thin too, this counts as an error.
            body, page_title, page_date = "", "", None
            if len(feed_text) < min_chars:
                rep.errors += 1
                if not rep.error_detail:
                    rep.error_detail = res.error
                log.debug("[%s] article fetch failed %s: %s", source.name, url, res.error)
                continue
            flags.append("page_%s" % (res.error or "unavailable"))

        if len(feed_text) > len(body):
            body = feed_text
            flags.append("body_from_feed")

        art = Article(
            url=res.final_url or url,
            outlet=source.name,
            tier=source.tier,
            title=title or page_title,
            body=body,
            published=published or page_date,
            summary=feed_text[:500],
            flags=flags,
        )
        rep.fetched += 1
        truncated = looks_truncated(art.body, truncation)
        if art.body_chars < min_chars or truncated:
            # Paywalled or JS-rendered. Logged and skipped: never sent to the model.
            rep.thin += 1
            reason = "paywall_teaser" if truncated else "thin_body"
            art.fetch_error = "%s_%d_chars" % (reason, art.body_chars)
            log.info("[%s] %s (%d chars), skipping: %s",
                     source.name, reason, art.body_chars, url)
            continue
        rep.articles.append(art)
    return rep


def ingest_all(cfg, fetcher, sources, limit: Optional[int] = None) -> List[SourceReport]:
    workers = int(cfg.get("fetch.max_domain_concurrency", 8))
    workers = max(1, min(workers, max(1, len(sources))))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(lambda s: ingest_source(cfg, fetcher, s, limit), sources))
