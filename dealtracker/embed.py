"""Local hashed term-frequency embeddings + shingle similarity.

Deliberately not an API embedding model:
  - it runs on every record every day and must be free,
  - and it must be byte-stable, because vectors are persisted in SQLite and
    compared against vectors written by earlier runs. A hosted model that
    silently reversions would corrupt 14 days of stored vectors.
"""
from __future__ import annotations

import hashlib
import math
import re
from typing import Iterable, List, Optional, Sequence, Set

import numpy as np

_TOKEN = re.compile(r"[a-z0-9]+")

MODEL_ID = "hash-tf-v1"


def _tokens(text: str) -> List[str]:
    words = _TOKEN.findall((text or "").lower())
    grams = list(words)
    grams.extend("%s_%s" % (a, b) for a, b in zip(words, words[1:]))
    return grams


def _bucket(token: str, dims: int) -> int:
    digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, "big") % dims


def embed(text: str, dims: int = 512) -> np.ndarray:
    vec = np.zeros(dims, dtype=np.float32)
    counts = {}
    for tok in _tokens(text):
        counts[tok] = counts.get(tok, 0) + 1
    for tok, n in counts.items():
        # Sublinear term frequency: one repeated word must not dominate.
        vec[_bucket(tok, dims)] += 1.0 + math.log(n)
    norm = float(np.linalg.norm(vec))
    return vec / norm if norm else vec


def cosine(a: np.ndarray, b: np.ndarray) -> float:
    if a is None or b is None or a.size == 0 or b.size == 0:
        return 0.0
    na, nb = float(np.linalg.norm(a)), float(np.linalg.norm(b))
    if not na or not nb:
        return 0.0
    return float(np.dot(a, b) / (na * nb))


def to_blob(vec: np.ndarray) -> bytes:
    return np.asarray(vec, dtype=np.float32).tobytes()


def from_blob(blob: Optional[bytes]) -> Optional[np.ndarray]:
    if not blob:
        return None
    return np.frombuffer(blob, dtype=np.float32)


def record_text(record, body: str = "", body_chars: int = 500) -> str:
    """What gets embedded: company + sector + the opening of the article body."""
    return " ".join(
        part for part in (
            record.company_name or "",
            record.sector or "",
            (body or record.body_excerpt or "")[:body_chars],
        ) if part
    ).strip()


# --- near-duplicate text detection (syndication gate) ---------------------

def shingles(text: str, size: int = 5) -> Set[int]:
    words = _TOKEN.findall((text or "").lower())
    if len(words) < size:
        return {hash(" ".join(words))} if words else set()
    return {
        hash(" ".join(words[i:i + size]))
        for i in range(len(words) - size + 1)
    }


def shingle_overlap(a: Set[int], b: Set[int]) -> float:
    """Containment, not Jaccard.

    A syndicated reprint is often the wire copy plus a boilerplate footer, so it
    is a superset. Jaccard would score that below threshold; containment of the
    smaller set in the larger one is what actually identifies a reprint.
    """
    if not a or not b:
        return 0.0
    smaller, larger = (a, b) if len(a) <= len(b) else (b, a)
    return len(smaller & larger) / float(len(smaller))


# --- backends -------------------------------------------------------------
#
# The stage-2 threshold is NOT portable across backends. 0.93 is a sensible
# number for a semantic embedding model, where two different fintech Series A
# rounds in the same week land around 0.80-0.90. This lexical backend puts the
# SAME deal from two outlets near 0.65, so 0.93 with `hash-tf-v1` means stage 2
# effectively never fires. That fails in the safe direction -- a missed merge is
# a visible duplicate row, a wrong merge is invisible -- but it does mean stage 2
# is off until you calibrate. `dealtracker run --no-dedup` prints the score
# distribution over your own sources so you can set a real value.

class HashTFBackend:
    model_id = MODEL_ID
    semantic = False

    def __init__(self, dims: int = 512):
        self.dims = dims

    def embed(self, text: str) -> np.ndarray:
        return embed(text, self.dims)

    def embed_many(self, texts: Sequence[str]) -> List[np.ndarray]:
        return [self.embed(t) for t in texts]


class VoyageBackend:
    """Optional semantic backend. Needs `voyageai` and VOYAGE_API_KEY."""

    semantic = True

    def __init__(self, model: str = "voyage-3", dims: int = 1024):
        import voyageai

        self.model_id = model
        self.dims = dims
        self._client = voyageai.Client()

    def embed(self, text: str) -> np.ndarray:
        return self.embed_many([text])[0]

    def embed_many(self, texts: Sequence[str]) -> List[np.ndarray]:
        result = self._client.embed(list(texts), model=self.model_id, input_type="document")
        return [np.asarray(v, dtype=np.float32) for v in result.embeddings]


def get_backend(cfg):
    name = str(cfg.get("dedupe.embedding_model", MODEL_ID))
    dims = int(cfg.get("dedupe.embedding_dims", 512))
    if name.startswith("voyage"):
        model = name.split(":", 1)[1] if ":" in name else "voyage-3"
        return VoyageBackend(model=model, dims=dims)
    return HashTFBackend(dims=dims)
