import asyncio
import functools
import operator
from dataclasses import dataclass

from sqlalchemy import ColumnElement, case, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from citadel.db import get_sessionmaker
from citadel.embedding import embed_texts
from citadel.models.content import ContentNode

RRF_K = 60
CANDIDATES = 50
TEXT_TOP = 30
TABLE_TOP = 30

TEXT_CHANNEL = ContentNode.type != "table"
TABLE_CHANNEL = ContentNode.type == "table"


@dataclass
class Retrieval:
    text: list[str]
    tables: list[str]


def _terms(queries: list[str]) -> list[str]:
    terms: list[str] = []
    seen: set[str] = set()
    for query in queries:
        for token in query.split():
            key = token.casefold()
            if key and key not in seen:
                seen.add(key)
                terms.append(token)
    return terms


def _like(term: str) -> str:
    escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


async def _dense(session: AsyncSession, channel: ColumnElement[bool], vector: list[float]) -> list[str]:
    stmt = (
        select(ContentNode.content_id)
        .where(channel, ContentNode.embedding.isnot(None))
        .order_by(ContentNode.embedding.cosine_distance(vector))
        .limit(CANDIDATES)
    )
    return list(await session.scalars(stmt))


async def _sparse(session: AsyncSession, channel: ColumnElement[bool], terms: list[str]) -> list[str]:
    if not terms:
        return []
    conditions = [ContentNode.search_text.ilike(_like(term), escape="\\") for term in terms]
    hits = functools.reduce(operator.add, (case((cond, 1), else_=0) for cond in conditions))
    stmt = (
        select(ContentNode.content_id)
        .where(channel, ContentNode.search_text.isnot(None), or_(*conditions))
        .order_by(hits.desc())
        .limit(CANDIDATES)
    )
    return list(await session.scalars(stmt))


def _rrf(rankings: list[list[str]]) -> list[str]:
    scores: dict[str, float] = {}
    for ranking in rankings:
        for rank, content_id in enumerate(ranking, start=1):
            scores[content_id] = scores.get(content_id, 0.0) + 1.0 / (RRF_K + rank)
    return sorted(scores, key=lambda content_id: scores[content_id], reverse=True)


async def _channel(
    session: AsyncSession, channel: ColumnElement[bool], vectors: list[list[float]], terms: list[str]
) -> list[str]:
    lists = [await _dense(session, channel, vector) for vector in vectors]
    lists.append(await _sparse(session, channel, terms))
    return _rrf(lists)


async def retrieve(queries: list[str]) -> Retrieval:
    vectors = await asyncio.to_thread(embed_texts, queries)
    terms = _terms(queries)
    async with get_sessionmaker()() as session:
        text = await _channel(session, TEXT_CHANNEL, vectors, terms)
        tables = await _channel(session, TABLE_CHANNEL, vectors, terms)
    return Retrieval(text=text[:TEXT_TOP], tables=tables[:TABLE_TOP])
