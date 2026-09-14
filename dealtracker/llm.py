"""One LLM call per surviving article, forced through a tool schema."""
from __future__ import annotations

import json
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from dealtracker import prompts
from dealtracker.records import DealRecord, build_record

log = logging.getLogger("dealtracker.llm")

# Cheap deterministic gate: titles that are unambiguously roundups never need a
# model call. The prompt still carries the rule, because most roundups are only
# detectable from the body.
DEFAULT_ROUNDUP_PATTERNS = [
    r"\bround\s*-?\s*up\b",
    r"\bweekly\b.*\b(funding|deals?|wrap)\b",
    r"\b(daily|weekly|monthly)\s+(digest|wrap|brief|dose)\b",
    r"\bthis\s+week\s+in\b",
    r"\bfunding\s+(roundup|wrap|tracker)\b",
    r"\bnews\s+and\s+updates\b",
    r"^\s*\d+\s+(startups?|companies|deals?)\b",
    r"\bstartups?\s+that\s+(raised|got funded)\b",
    r"&\s+more\s*$",
]


@dataclass
class ExtractionStats:
    attempted: int = 0
    succeeded: int = 0
    failed: int = 0
    skipped_roundup: int = 0
    deal_none: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    errors: List[str] = field(default_factory=list)


class Extractor:
    def __init__(self, cfg, client=None):
        self.cfg = cfg
        self.model = cfg.get("extraction.model", "claude-haiku-4-5-20251001")
        self.max_tokens = int(cfg.get("extraction.max_tokens", 1200))
        self.temperature = float(cfg.get("extraction.temperature", 0.0))
        self.max_body = int(cfg.get("extraction.max_body_chars", 12000))
        self.fx = cfg.inr_per_usd
        self.retries = int(cfg.get("extraction.retries", 2))
        patterns = cfg.get("extraction.roundup_title_patterns") or DEFAULT_ROUNDUP_PATTERNS
        self.roundup_rx = [re.compile(p, re.I) for p in patterns]
        self._client = client

    @property
    def client(self):
        if self._client is None:
            import anthropic

            self._client = anthropic.Anthropic()
        return self._client

    def is_obvious_roundup(self, article) -> bool:
        title = article.title or ""
        return any(rx.search(title) for rx in self.roundup_rx)

    def _call(self, article) -> Tuple[Optional[Dict[str, Any]], Optional[Any], str]:
        user = prompts.build_user_message(article, self.fx, self.max_body)
        last_error = ""
        for attempt in range(self.retries + 1):
            try:
                msg = self.client.messages.create(
                    model=self.model,
                    max_tokens=self.max_tokens,
                    temperature=self.temperature,
                    system=prompts.SYSTEM,
                    tools=[prompts.TOOL],
                    tool_choice={"type": "tool", "name": prompts.TOOL["name"]},
                    messages=[{"role": "user", "content": user}],
                )
            except Exception as exc:  # noqa: BLE001
                last_error = "%s: %s" % (type(exc).__name__, str(exc)[:160])
                if attempt < self.retries:
                    time.sleep(2.0 * (2 ** attempt))
                continue
            for block in msg.content:
                if getattr(block, "type", "") == "tool_use":
                    return dict(block.input), msg.usage, ""
            # Forced tool_choice should make this unreachable; handle it anyway.
            text = "".join(getattr(b, "text", "") for b in msg.content)
            payload = _loads_json(text)
            if payload is not None:
                return payload, msg.usage, ""
            last_error = "no_tool_use_block"
        return None, None, last_error or "unknown_error"

    def extract(self, article) -> Tuple[Optional[DealRecord], str]:
        payload, usage, error = self._call(article)
        if payload is None:
            return None, error
        record = build_record(payload, article, self.fx)
        record.llm_usage = usage  # type: ignore[attr-defined]
        return record, ""


def _loads_json(text: str) -> Optional[Dict[str, Any]]:
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.M).strip()
    try:
        return json.loads(text)
    except ValueError:
        m = re.search(r"\{.*\}", text, re.S)
        if not m:
            return None
        try:
            return json.loads(m.group(0))
        except ValueError:
            return None


def extract_many(cfg, articles, client=None, on_result=None):
    """-> (records, stats). Records include deal_type 'none' so they can be shown."""
    ex = Extractor(cfg, client=client)
    stats = ExtractionStats()
    workers = max(1, int(cfg.get("extraction.max_concurrency", 4)))

    todo = []
    for art in articles:
        if ex.is_obvious_roundup(art):
            stats.skipped_roundup += 1
            log.info("roundup title, no model call: %s", art.title[:90])
            continue
        todo.append(art)

    def run(art):
        rec, err = ex.extract(art)
        return art, rec, err

    records: List[DealRecord] = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for art, rec, err in pool.map(run, todo):
            stats.attempted += 1
            if rec is None:
                stats.failed += 1
                stats.errors.append("%s: %s" % (art.outlet, err))
                log.warning("extraction failed [%s] %s: %s", art.outlet, art.url, err)
                continue
            stats.succeeded += 1
            usage = getattr(rec, "llm_usage", None)
            if usage is not None:
                stats.input_tokens += getattr(usage, "input_tokens", 0) or 0
                stats.output_tokens += getattr(usage, "output_tokens", 0) or 0
                try:
                    delattr(rec, "llm_usage")
                except AttributeError:
                    pass
            if rec.deal_type == "none":
                stats.deal_none += 1
            records.append(rec)
            if on_result:
                on_result(art, rec)
    return records, stats
