import asyncio

from sqlalchemy import func, select

from citadel.db import get_sessionmaker
from citadel.llm import call_embed
from citadel.models.content import ContentNode
from citadel.models.embedding import Embedding

RRF_K = 60


async def dense_rank(query: str, content_ids: list[int], key: str) -> list[int]:
    if not content_ids:
        return []
    vector = (await call_embed([query], key))[0]
    async with get_sessionmaker()() as session:
        rows = await session.execute(
            select(Embedding.content_id)
            .where(Embedding.content_id.in_(content_ids))
            .order_by(Embedding.embedding.cosine_distance(vector))
        )
    return [row[0] for row in rows]


async def sparse_rank(query: str, content_ids: list[int]) -> list[int]:
    if not content_ids:
        return []
    async with get_sessionmaker()() as session:
        rows = await session.execute(
            select(ContentNode.id)
            .where(ContentNode.id.in_(content_ids), ContentNode.search_text.isnot(None))
            .order_by(func.similarity(ContentNode.search_text, query).desc())
        )
    return [row[0] for row in rows]


def rrf_fuse(rankings: list[list[int]]) -> list[int]:
    scores: dict[int, float] = {}
    for ranking in rankings:
        for rank, content_id in enumerate(ranking):
            scores[content_id] = scores.get(content_id, 0.0) + 1.0 / (RRF_K + rank + 1)
    return sorted(scores, key=lambda content_id: scores[content_id], reverse=True)


async def rank_content(query: str, content_ids: list[int], key: str) -> list[int]:
    dense, sparse = await asyncio.gather(dense_rank(query, content_ids, key), sparse_rank(query, content_ids))
    return rrf_fuse([dense, sparse])
