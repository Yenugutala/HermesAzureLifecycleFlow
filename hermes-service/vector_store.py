"""
In-memory vector store for Confluence pages.

Loads once at startup from data/confluence_index.json (built by scripts/build_index.py).
Provides cosine similarity search using pure numpy — no external database needed.

Usage:
    import vector_store
    vector_store.load_index()          # call once at startup
    results = vector_store.search(embedding, top_k=3)
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)

_INDEX_PATH = Path(__file__).parent / "data" / "confluence_index.json"

# Module-level state — loaded once at startup, shared across all requests
_pages: list[dict] = []
_matrix: np.ndarray | None = None   # shape (N, embedding_dim)


def _cosine_sim(query_vec: np.ndarray, matrix: np.ndarray) -> np.ndarray:
    """Cosine similarity between a 1-D query vector and each row of matrix."""
    q = query_vec / (np.linalg.norm(query_vec) + 1e-9)
    m = matrix / (np.linalg.norm(matrix, axis=1, keepdims=True) + 1e-9)
    return m @ q   # shape (N,)


def load_index() -> None:
    """Load confluence_index.json into RAM. Call once at service startup."""
    global _pages, _matrix
    if not _INDEX_PATH.exists():
        log.warning(
            "confluence_index.json not found at %s — vector search disabled. "
            "Run: python scripts/build_index.py",
            _INDEX_PATH,
        )
        return
    with open(_INDEX_PATH) as f:
        _pages = json.load(f)
    if not _pages:
        log.warning("confluence_index.json is empty — vector search disabled")
        return
    embeddings = [p["embedding"] for p in _pages]
    _matrix = np.array(embeddings, dtype=np.float32)
    log.info("Loaded %d Confluence pages into vector store", len(_pages))


def search(query_embedding: list[float], top_k: int = 3) -> list[dict]:
    """
    Return the top-k most similar Confluence pages for a given query embedding.
    Returns an empty list if the index has not been loaded.

    Each result dict contains: page_title, space_key, page_url, content_snippet, score.
    """
    if _matrix is None or len(_pages) == 0:
        return []
    q = np.array(query_embedding, dtype=np.float32)
    scores = _cosine_sim(q, _matrix)
    top_indices = np.argsort(scores)[::-1][:top_k]
    results = []
    for idx in top_indices:
        p = _pages[int(idx)]
        results.append({
            "page_title":      p["page_title"],
            "space_key":       p.get("space_key", ""),
            "page_url":        p.get("page_url", ""),
            "content_snippet": p.get("content_snippet", ""),
            "score":           float(scores[idx]),
        })
    return results
