import json
import re
from collections import defaultdict
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import numpy as np
import torch
from rank_bm25 import BM25Okapi
from sentence_transformers import CrossEncoder, SentenceTransformer

from config import get_settings

NOISE_TYPES = {"footer", "page_number"}  # boilerplate that just adds noise to retrieval
RRF_K = 60  # reciprocal-rank-fusion damping constant


@dataclass
class Chunk:
    text: str
    source: str
    page_idx: int
    type: str


@lru_cache
def get_embedder() -> SentenceTransformer:
    # BGE-M3 bi-encoder (dense channel) on the GPU in bf16, inside the reserved budget
    return SentenceTransformer("BAAI/bge-m3", device="cuda", model_kwargs={"torch_dtype": torch.bfloat16})


@lru_cache
def get_reranker() -> CrossEncoder:
    # cross-encoder: reranks the fused shortlist (query, chunk) pairs jointly
    return CrossEncoder("BAAI/bge-reranker-v2-m3", device="cuda", model_kwargs={"torch_dtype": torch.bfloat16})


def _tokenize(text: str) -> list[str]:
    return re.findall(r"\w+", text.lower())


def _load_chunks() -> list[Chunk]:
    # the results dir holds many re-ingest runs → keep the newest result per source, one chunk per real block
    results_dir = get_settings().data_dir / "results"
    newest: dict[str, Path] = {}
    for path in sorted(results_dir.glob("*.json"), key=lambda p: p.stat().st_mtime):
        newest[json.loads(path.read_text())["source"]] = path
    chunks: list[Chunk] = []
    for path in newest.values():
        doc = json.loads(path.read_text())
        for block in doc["blocks"]:
            text = block["text"].strip()
            if text and block["type"] not in NOISE_TYPES:
                chunks.append(Chunk(text, doc["source"], block["page_idx"], block["type"]))
    return chunks


@lru_cache
def get_index() -> tuple[np.ndarray, BM25Okapi, tuple[Chunk, ...]]:
    chunks = _load_chunks()
    vectors = np.asarray(get_embedder().encode([c.text for c in chunks], normalize_embeddings=True, batch_size=64))
    bm25 = BM25Okapi([_tokenize(c.text) for c in chunks])
    return vectors, bm25, tuple(chunks)


def _rrf(rankings: list[list[int]]) -> list[int]:
    # reciprocal rank fusion: each item scored by sum of 1/(RRF_K + rank) across the input rankings
    scores: dict[int, float] = defaultdict(float)
    for ranking in rankings:
        for rank, idx in enumerate(ranking):
            scores[idx] += 1.0 / (RRF_K + rank)
    return sorted(scores, key=lambda i: scores[i], reverse=True)


def search(query: str, per_channel: int = 100, fuse_top: int = 50, top: int = 10) -> list[dict]:
    # dense (BGE-M3) + sparse (BM25) → RRF fuse → cross-encoder rerank the fused shortlist
    vectors, bm25, chunks = get_index()
    q = get_embedder().encode(query, normalize_embeddings=True)
    dense = [int(i) for i in np.argsort(vectors @ q)[::-1][:per_channel]]
    sparse = [int(i) for i in np.argsort(bm25.get_scores(_tokenize(query)))[::-1][:per_channel]]
    fused = _rrf([dense, sparse])[:fuse_top]
    rerank = get_reranker().predict([(query, chunks[i].text) for i in fused])
    ranked = sorted(zip(fused, rerank, strict=True), key=lambda pair: pair[1], reverse=True)[:top]
    return [
        {
            "score": float(score),
            "source": chunks[i].source,
            "page": chunks[i].page_idx,
            "type": chunks[i].type,
            "text": chunks[i].text,
        }
        for i, score in ranked
    ]
