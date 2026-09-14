"""SQLite state: the last N days of records, their vectors, and article hashes.

The Sheet is append-only output, never an input. Everything the pipeline needs
to decide "have I seen this deal before?" lives here, so a run never re-reads
the Sheet.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

import numpy as np

from dealtracker.embed import from_blob, to_blob
from dealtracker.records import DealRecord

log = logging.getLogger("dealtracker.store")

SCHEMA = """
CREATE TABLE IF NOT EXISTS deals (
    deal_id      TEXT PRIMARY KEY,
    deal_type    TEXT NOT NULL,
    deal_date    TEXT,
    date_added   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    fingerprint  TEXT,
    sheet_row    INTEGER,
    payload      TEXT NOT NULL,
    vector       BLOB,
    vector_model TEXT
);
CREATE INDEX IF NOT EXISTS deals_date ON deals(deal_date);
CREATE INDEX IF NOT EXISTS deals_added ON deals(date_added);

CREATE TABLE IF NOT EXISTS articles (
    article_id  TEXT PRIMARY KEY,
    url         TEXT NOT NULL,
    outlet      TEXT,
    seen_at     TEXT NOT NULL,
    body_chars  INTEGER,
    shingles    BLOB
);
CREATE INDEX IF NOT EXISTS articles_seen ON articles(seen_at);

CREATE TABLE IF NOT EXISTS runs (
    run_id     TEXT PRIMARY KEY,
    started_at TEXT,
    summary    TEXT
);
"""


class Store:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path))
        self.conn.row_factory = sqlite3.Row
        with closing(self.conn.cursor()) as cur:
            cur.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # -- deals ------------------------------------------------------
    def load_recent(self, days: int) -> Tuple[List[DealRecord], Dict[str, np.ndarray]]:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        rows = self.conn.execute(
            "SELECT * FROM deals WHERE date_added >= ? OR (deal_date IS NOT NULL AND deal_date >= ?)",
            (cutoff, cutoff[:10]),
        ).fetchall()
        records, vectors = [], {}
        for row in rows:
            rec = DealRecord(**json.loads(row["payload"]))
            rec.deal_id = row["deal_id"]
            records.append(rec)
            vec = from_blob(row["vector"])
            if vec is not None:
                vectors[rec.deal_id] = vec
        return records, vectors

    def sheet_row_for(self, deal_id: str) -> Optional[int]:
        row = self.conn.execute(
            "SELECT sheet_row FROM deals WHERE deal_id = ?", (deal_id,)
        ).fetchone()
        return row["sheet_row"] if row and row["sheet_row"] else None

    def upsert(self, records: Iterable[DealRecord], vectors=None, vector_model="",
               fingerprints=None) -> None:
        vectors = vectors or {}
        fingerprints = fingerprints or {}
        now = datetime.now(timezone.utc).isoformat()
        rows = []
        for rec in records:
            payload = rec.to_dict()
            payload.pop("deal_id", None)
            vec = vectors.get(rec.deal_id)
            rows.append((
                rec.deal_id, rec.deal_type, rec.deal_date, rec.date_added or now, now,
                fingerprints.get(rec.deal_id) or rec.fingerprint,
                rec.__dict__.get("sheet_row"),
                json.dumps(payload, ensure_ascii=False),
                to_blob(vec) if vec is not None else None,
                vector_model,
            ))
        with closing(self.conn.cursor()) as cur:
            cur.executemany(
                """INSERT INTO deals
                   (deal_id, deal_type, deal_date, date_added, updated_at, fingerprint,
                    sheet_row, payload, vector, vector_model)
                   VALUES (?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(deal_id) DO UPDATE SET
                     deal_type=excluded.deal_type,
                     deal_date=excluded.deal_date,
                     updated_at=excluded.updated_at,
                     fingerprint=excluded.fingerprint,
                     sheet_row=COALESCE(excluded.sheet_row, deals.sheet_row),
                     payload=excluded.payload,
                     vector=COALESCE(excluded.vector, deals.vector),
                     vector_model=excluded.vector_model""",
                rows,
            )
        self.conn.commit()

    def set_sheet_row(self, deal_id: str, row_number: int) -> None:
        self.conn.execute("UPDATE deals SET sheet_row = ? WHERE deal_id = ?",
                          (row_number, deal_id))
        self.conn.commit()

    # -- articles (near-duplicate gate) ------------------------------
    def seen_article_ids(self) -> Set[str]:
        return {r["article_id"] for r in self.conn.execute("SELECT article_id FROM articles")}

    def recent_shingles(self, hours: int) -> List[Tuple[str, str, Set[int]]]:
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        out = []
        for row in self.conn.execute(
            "SELECT article_id, url, shingles FROM articles WHERE seen_at >= ? AND shingles IS NOT NULL",
            (cutoff,),
        ):
            arr = np.frombuffer(row["shingles"], dtype=np.int64)
            out.append((row["article_id"], row["url"], set(arr.tolist())))
        return out

    def record_articles(self, entries: Iterable[Tuple[Any, Optional[Set[int]]]]) -> None:
        now = datetime.now(timezone.utc).isoformat()
        rows = []
        for article, sh in entries:
            blob = np.asarray(sorted(sh), dtype=np.int64).tobytes() if sh else None
            rows.append((article.article_id, article.url, article.outlet, now,
                         article.body_chars, blob))
        with closing(self.conn.cursor()) as cur:
            cur.executemany(
                """INSERT INTO articles (article_id, url, outlet, seen_at, body_chars, shingles)
                   VALUES (?,?,?,?,?,?)
                   ON CONFLICT(article_id) DO UPDATE SET seen_at=excluded.seen_at""",
                rows,
            )
        self.conn.commit()

    # -- housekeeping ------------------------------------------------
    def log_run(self, run_id: str, summary: Dict[str, Any]) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO runs (run_id, started_at, summary) VALUES (?,?,?)",
            (run_id, datetime.now(timezone.utc).isoformat(),
             json.dumps(summary, ensure_ascii=False)),
        )
        self.conn.commit()

    def prune(self, retention_days: int) -> Dict[str, int]:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).isoformat()
        with closing(self.conn.cursor()) as cur:
            cur.execute("DELETE FROM deals WHERE date_added < ?", (cutoff,))
            deals = cur.rowcount
            cur.execute("DELETE FROM articles WHERE seen_at < ?", (cutoff,))
            arts = cur.rowcount
            cur.execute("DELETE FROM runs WHERE started_at < ?", (cutoff,))
        self.conn.commit()
        self.conn.execute("VACUUM")
        return {"deals": max(deals, 0), "articles": max(arts, 0)}
