"""Article model + body-text extraction."""
from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from bs4 import BeautifulSoup

# Always removed: never contain article prose.
HARD_STRIP_TAGS = ["script", "style", "noscript", "iframe", "svg", "button", "form"]

# Usually chrome, but some CMSes wrap the article in one of these. Removed only
# when the node is not itself carrying the article -- see _safe_decompose.
SOFT_STRIP_TAGS = ["nav", "aside", "footer", "header", "figure", "figcaption"]

# Wrappers that sit inside the article container but are not the article.
# These are substring matches on class names, so they can and do match an
# ancestor of the real content (Inc42 wraps <article> in a *-share div).
# _safe_decompose refuses to delete any node that is carrying real prose.
STRIP_SELECTORS = [
    "[class*='related']", "[class*='Related']", "[class*='newsletter']",
    "[class*='subscribe']", "[class*='promo']", "[class*='advert']",
    "[class*='trending']", "[class*='also-read']", "[class*='alsoRead']",
    "[class*='breadcrumb']", "[class*='share']", "[class*='tags']",
    "[id*='taboola']", "[class*='taboola']", "[class*='comment']",
]

_WS = re.compile(r"[ \t\xa0]+")
_NL = re.compile(r"\n{3,}")


@dataclass
class Article:
    url: str
    outlet: str
    tier: int
    title: str = ""
    body: str = ""
    published: Optional[str] = None      # ISO8601 UTC
    summary: str = ""                    # feed summary, used by the relevance filter
    fetch_error: str = ""
    flags: list = field(default_factory=list)

    @property
    def article_id(self) -> str:
        return hashlib.sha1(self.url.encode("utf-8")).hexdigest()[:16]

    @property
    def first_para(self) -> str:
        for chunk in self.body.split("\n"):
            chunk = chunk.strip()
            if len(chunk) > 60:
                return chunk
        return self.summary or self.body[:400]

    @property
    def body_chars(self) -> int:
        return len(self.body)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["article_id"] = self.article_id
        return d


# A node holding more prose than this is content, not chrome, whatever its class.
PROTECT_PROSE_CHARS = 400


def _prose_len(node) -> int:
    try:
        return sum(len(p.get_text(" ", strip=True)) for p in node.find_all("p"))
    except Exception:  # noqa: BLE001 - node may already be detached
        return 0


def _safe_decompose(soup, selectors, by_name: bool = False) -> None:
    for sel in selectors:
        try:
            nodes = soup(sel) if by_name else soup.select(sel)
        except Exception:  # noqa: BLE001
            continue
        for node in nodes:
            if node.decomposed or _prose_len(node) > PROTECT_PROSE_CHARS:
                continue
            node.decompose()


def _clean_text(text: str) -> str:
    text = _WS.sub(" ", text)
    lines = [ln.strip() for ln in text.splitlines()]
    return _NL.sub("\n\n", "\n".join(ln for ln in lines if ln))


def _dedupe_paragraphs(parts):
    """Drop paragraphs the page renders twice.

    The standfirst is often a *prefix* of the body's opening paragraph rather
    than an exact copy, so equality on a fixed-length key misses it. Compare by
    containment and keep the longer version.
    """
    kept: list = []          # (normalised, original)
    for part in parts:
        norm = re.sub(r"\W+", "", part.lower())
        if len(norm) < 40:
            kept.append((norm, part))
            continue
        replaced = False
        drop = False
        for i, (seen_norm, _) in enumerate(kept):
            if len(seen_norm) < 40:
                continue
            if norm in seen_norm:
                drop = True
                break
            if seen_norm in norm:
                kept[i] = (norm, part)   # keep the fuller paragraph
                replaced = True
                break
        if not drop and not replaced:
            kept.append((norm, part))
    return [orig for _, orig in kept]


def _text_of(node, boilerplate=None) -> str:
    parts = []
    for p in node.find_all(["p", "h2", "h3", "li"]):
        t = p.get_text(" ", strip=True)
        if t:
            parts.append(t)
    parts = _drop_boilerplate(_dedupe_paragraphs(parts), boilerplate)
    if not parts:
        return _clean_text(node.get_text("\n", strip=True))
    return _clean_text("\n".join(parts))


def compile_patterns(patterns) -> list:
    out = []
    for pat in patterns or []:
        try:
            out.append(re.compile(pat, re.I))
        except re.error:
            continue
    return out


def _drop_boilerplate(parts, patterns):
    if not patterns:
        return parts
    return [p for p in parts if not any(rx.search(p) for rx in patterns)]


def looks_truncated(body: str, markers) -> bool:
    tail = (body or "").strip()[-40:]
    return any(rx.search(tail) for rx in markers or [])


def extract_body(html: str, body_selector: str = "", boilerplate=None) -> str:
    """Selector-first, with a generic paragraph-density fallback.

    Paywalled outlets legitimately return a couple of paragraphs here; that is
    handled downstream by the min_body_chars gate, not by guessing.
    """
    if not html:
        return ""
    boilerplate = boilerplate or []
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(HARD_STRIP_TAGS):
        tag.decompose()
    _safe_decompose(soup, SOFT_STRIP_TAGS, by_name=True)
    _safe_decompose(soup, STRIP_SELECTORS)

    best = ""
    if body_selector:
        for sel in [s.strip() for s in body_selector.split(",") if s.strip()]:
            try:
                nodes = soup.select(sel)
            except Exception:  # noqa: BLE001 - a bad selector must not kill the run
                continue
            for node in nodes:
                text = _text_of(node, boilerplate)
                if len(text) > len(best):
                    best = text
    if len(best) >= 200:
        return best

    # Fallback: the densest block of <p> text on the page.
    paras = [p.get_text(" ", strip=True) for p in soup.find_all("p")]
    paras = _drop_boilerplate(_dedupe_paragraphs([p for p in paras if len(p) > 40]), boilerplate)
    fallback = _clean_text("\n".join(paras))
    return fallback if len(fallback) > len(best) else best


def extract_title(html: str, selector: str = "h1") -> str:
    if not html:
        return ""
    soup = BeautifulSoup(html, "lxml")
    for sel in [s.strip() for s in (selector or "h1").split(",") if s.strip()]:
        try:
            node = soup.select_one(sel)
        except Exception:  # noqa: BLE001
            continue
        if node:
            t = node.get_text(" ", strip=True)
            if t:
                return t
    og = soup.find("meta", property="og:title")
    if og and og.get("content"):
        return og["content"].strip()
    if soup.title and soup.title.string:
        return soup.title.string.strip()
    return ""


def extract_published(html: str) -> Optional[str]:
    if not html:
        return None
    soup = BeautifulSoup(html, "lxml")
    for prop in ("article:published_time", "article:modified_time", "og:published_time"):
        meta = soup.find("meta", property=prop)
        if meta and meta.get("content"):
            iso = parse_date(meta["content"])
            if iso:
                return iso
    for meta in soup.find_all("meta", attrs={"name": True}):
        if meta["name"].lower() in ("pubdate", "publishdate", "publish-date", "date"):
            iso = parse_date(meta.get("content", ""))
            if iso:
                return iso
    t = soup.find("time", attrs={"datetime": True})
    if t:
        return parse_date(t["datetime"])
    return None


def parse_date(value: Any) -> Optional[str]:
    if not value:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        from dateutil import parser as dparser

        try:
            dt = dparser.parse(str(value))
        except (ValueError, OverflowError, TypeError):
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()
