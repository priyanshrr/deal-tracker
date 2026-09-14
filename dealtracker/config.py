"""Config loading. Everything tunable lives in YAML, not here."""
from __future__ import annotations

import copy
import re
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent


class Source(dict):
    """A source block from sources.yaml, with defaults already merged in."""

    @property
    def name(self) -> str:
        return self["name"]

    @property
    def domain(self) -> str:
        from urllib.parse import urlparse

        return urlparse(self["url"]).netloc.lower()

    @property
    def tier(self) -> int:
        return int(self["tier"])

    @property
    def enabled(self) -> bool:
        return bool(self.get("enabled", False))

    @property
    def flags(self) -> List[str]:
        return list(self.get("flags") or [])

    def selector(self, key: str) -> str:
        return (self.get("selectors") or {}).get(key) or ""


class Config:
    def __init__(self, config_path: Path, sources_path: Path):
        self.config_path = config_path
        self.sources_path = sources_path
        self.raw: Dict[str, Any] = yaml.safe_load(config_path.read_text()) or {}
        self._sources_doc: Dict[str, Any] = yaml.safe_load(sources_path.read_text()) or {}
        self.sources: List[Source] = self._build_sources()

    # -- sources ----------------------------------------------------
    def _build_sources(self) -> List[Source]:
        defaults = self._sources_doc.get("defaults") or {}
        default_selectors = defaults.get("selectors") or {}
        out: List[Source] = []
        for block in self._sources_doc.get("sources") or []:
            merged = copy.deepcopy(block)
            selectors = dict(default_selectors)
            selectors.update(merged.get("selectors") or {})
            merged["selectors"] = selectors
            merged["exclude_url_patterns"] = list(
                dict.fromkeys(
                    list(defaults.get("exclude_url_patterns") or [])
                    + list(merged.get("exclude_url_patterns") or [])
                )
            )
            out.append(Source(merged))
        return out

    def enabled_sources(
        self,
        only: Optional[List[str]] = None,
        tiers: Optional[List[int]] = None,
    ) -> List[Source]:
        picked = []
        for s in self.sources:
            if only:
                # --source overrides `enabled`: explicit beats config.
                if s.name.lower() not in {o.lower() for o in only}:
                    continue
            elif not s.enabled:
                continue
            if tiers and s.tier not in tiers:
                continue
            picked.append(s)
        return picked

    def save_sources(self) -> None:
        """Patch `type:`/`feed:` in place so hand-written comments survive.

        sources.yaml is a file a human maintains. Round-tripping it through
        yaml.safe_dump would silently delete every comment in it, so discovery
        edits only the lines it owns.
        """
        lines = self.sources_path.read_text().splitlines()
        wanted = {
            b.get("name"): {"type": b.get("type"), "feed": b.get("feed")}
            for b in (self._sources_doc.get("sources") or [])
        }
        current: Optional[str] = None
        name_re = re.compile(r"^(\s*)-\s+name:\s*(.+?)\s*$")
        out = []
        for line in lines:
            m = name_re.match(line)
            if m:
                current = m.group(2).strip().strip("'\"")
                out.append(line)
                continue
            if current and current in wanted:
                for key in ("type", "feed"):
                    km = re.match(r"^(\s*)%s:\s" % key, line)
                    if km:
                        val = wanted[current][key]
                        rendered = "null" if val is None else str(val)
                        line = "%s%s: %s" % (km.group(1), key, rendered)
                        break
            out.append(line)
        self.sources_path.write_text("\n".join(out) + "\n")

    def set_source_field(self, name: str, key: str, value: Any) -> None:
        for block in self._sources_doc.get("sources") or []:
            if block.get("name") == name:
                block[key] = value
        for s in self.sources:
            if s.name == name:
                s[key] = value

    # -- dotted access ----------------------------------------------
    def get(self, dotted: str, default: Any = None) -> Any:
        node: Any = self.raw
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def path(self, dotted: str, default: str = "") -> Path:
        value = self.get(dotted, default)
        p = Path(value)
        return p if p.is_absolute() else REPO_ROOT / p

    @property
    def inr_per_usd(self) -> float:
        return float(self.get("fx.inr_per_usd", 88.0))


def load_config(
    config_path: Optional[str] = None, sources_path: Optional[str] = None
) -> Config:
    cfg = Path(config_path or os.environ.get("DEALTRACKER_CONFIG") or REPO_ROOT / "config.yaml")
    src = Path(sources_path or os.environ.get("DEALTRACKER_SOURCES") or REPO_ROOT / "sources.yaml")
    return Config(cfg, src)
