"""Polite HTTP: robots.txt, one request per second per domain, bounded retries.

A dead source must never fail the run, so every public call here returns a
result object rather than raising.
"""
from __future__ import annotations

import logging
import threading
import time
import urllib.robotparser as robotparser
from dataclasses import dataclass
from typing import Dict, Optional
from urllib.parse import urljoin, urlparse

import requests

log = logging.getLogger("dealtracker.http")


@dataclass
class FetchResult:
    url: str
    ok: bool
    status: Optional[int] = None
    text: str = ""
    content_type: str = ""
    error: str = ""
    final_url: str = ""
    blocked_by_robots: bool = False


class _DomainClock:
    """Serialises requests per domain and enforces the inter-request delay."""

    def __init__(self, delay: float, overrides: Optional[Dict[str, float]] = None):
        self.delay = delay
        self.overrides = {k.lower().lstrip("www."): v for k, v in (overrides or {}).items()}
        self._lock = threading.Lock()
        self._locks: Dict[str, threading.Lock] = {}
        self._last: Dict[str, float] = {}

    def _lock_for(self, domain: str) -> threading.Lock:
        with self._lock:
            return self._locks.setdefault(domain, threading.Lock())

    def wait(self, domain: str) -> "_DomainSlot":
        return _DomainSlot(self, domain)

    def delay_for(self, domain: str) -> float:
        return self.overrides.get(domain.lower().lstrip("www."), self.delay)

    def _acquire(self, domain: str) -> None:
        self._lock_for(domain).acquire()
        last = self._last.get(domain, 0.0)
        gap = time.monotonic() - last
        delay = self.delay_for(domain)
        if gap < delay:
            time.sleep(delay - gap)

    def _release(self, domain: str) -> None:
        self._last[domain] = time.monotonic()
        lock = self._lock_for(domain)
        if lock.locked():
            lock.release()


class _DomainSlot:
    def __init__(self, clock: _DomainClock, domain: str):
        self.clock, self.domain = clock, domain

    def __enter__(self):
        self.clock._acquire(self.domain)
        return self

    def __exit__(self, *exc):
        self.clock._release(self.domain)
        return False


class Fetcher:
    def __init__(
        self,
        user_agent: str,
        respect_robots: bool = True,
        per_domain_delay_sec: float = 1.0,
        timeout_sec: int = 20,
        retries: int = 2,
        backoff_base_sec: float = 2.0,
        delay_overrides: Optional[Dict[str, float]] = None,
    ):
        self.user_agent = user_agent
        self.respect_robots = respect_robots
        self.timeout = timeout_sec
        self.retries = retries
        self.backoff = backoff_base_sec
        self.clock = _DomainClock(per_domain_delay_sec, delay_overrides)
        self._robots: Dict[str, Optional[robotparser.RobotFileParser]] = {}
        self._robots_lock = threading.Lock()
        self._local = threading.local()

    # -- session per thread -----------------------------------------
    @property
    def session(self) -> requests.Session:
        s = getattr(self._local, "session", None)
        if s is None:
            s = requests.Session()
            s.headers.update(
                {
                    "User-Agent": self.user_agent,
                    "Accept": "text/html,application/xhtml+xml,application/xml,"
                    "application/rss+xml;q=0.9,*/*;q=0.8",
                    "Accept-Language": "en-IN,en;q=0.9",
                }
            )
            self._local.session = s
        return s

    # -- robots ------------------------------------------------------
    def _robots_for(self, url: str) -> Optional[robotparser.RobotFileParser]:
        parts = urlparse(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        with self._robots_lock:
            if origin in self._robots:
                return self._robots[origin]
            self._robots[origin] = None  # placeholder: don't refetch on failure
        rp = robotparser.RobotFileParser()
        robots_url = urljoin(origin, "/robots.txt")
        try:
            with self.clock.wait(parts.netloc):
                resp = self.session.get(robots_url, timeout=self.timeout)
            if resp.status_code >= 400:
                # No robots.txt (or unreadable) is a permissive answer by convention.
                rp.parse([])
            else:
                rp.parse(resp.text.splitlines())
        except Exception as exc:  # noqa: BLE001 - never fail a run on robots
            log.debug("robots fetch failed for %s: %s", origin, exc)
            rp.parse([])
        with self._robots_lock:
            self._robots[origin] = rp
        return rp

    def allowed(self, url: str) -> bool:
        if not self.respect_robots:
            return True
        rp = self._robots_for(url)
        if rp is None:
            return True
        try:
            return rp.can_fetch(self.user_agent, url)
        except Exception:  # noqa: BLE001
            return True

    # -- fetch -------------------------------------------------------
    def get(self, url: str, allow_redirects: bool = True) -> FetchResult:
        if not self.allowed(url):
            log.info("robots.txt disallows %s", url)
            return FetchResult(url=url, ok=False, error="robots_disallow", blocked_by_robots=True)

        domain = urlparse(url).netloc
        last_error = ""
        last_status: Optional[int] = None
        for attempt in range(self.retries + 1):
            try:
                with self.clock.wait(domain):
                    resp = self.session.get(
                        url, timeout=self.timeout, allow_redirects=allow_redirects
                    )
                last_status = resp.status_code
                if resp.status_code == 200:
                    return FetchResult(
                        url=url,
                        ok=True,
                        status=200,
                        text=resp.text,
                        content_type=resp.headers.get("Content-Type", ""),
                        final_url=resp.url,
                    )
                if resp.status_code in (400, 401, 403, 404, 410):
                    # Not worth retrying; paywall/redirect walls answer this way too.
                    return FetchResult(
                        url=url, ok=False, status=resp.status_code, error=f"http_{resp.status_code}"
                    )
                last_error = f"http_{resp.status_code}"
            except requests.RequestException as exc:
                last_error = type(exc).__name__
            if attempt < self.retries:
                time.sleep(self.backoff * (2 ** attempt))
        log.info("giving up on %s after %d attempts (%s)", url, self.retries + 1, last_error)
        return FetchResult(url=url, ok=False, status=last_status, error=last_error or "unknown")


def build_fetcher(cfg) -> Fetcher:
    # Sources may ask for a gentler cadence than the global default
    # (`fetch_delay_sec` in sources.yaml) when a host rate-limits us.
    overrides = {}
    for s in getattr(cfg, "sources", []):
        if s.get("fetch_delay_sec"):
            overrides[s.domain] = float(s["fetch_delay_sec"])
    return Fetcher(
        delay_overrides=overrides,
        user_agent=cfg.get("fetch.user_agent", "IndianDealTracker/0.1"),
        respect_robots=bool(cfg.get("fetch.respect_robots", True)),
        per_domain_delay_sec=float(cfg.get("fetch.per_domain_delay_sec", 1.0)),
        timeout_sec=int(cfg.get("fetch.timeout_sec", 20)),
        retries=int(cfg.get("fetch.retries", 2)),
        backoff_base_sec=float(cfg.get("fetch.backoff_base_sec", 2.0)),
    )
