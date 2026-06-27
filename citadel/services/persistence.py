from collections.abc import AsyncIterator

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from citadel.db import get_sessionmaker
from citadel.models.content import Code, ContentNode, Equation, Heading, ListBlock, Paragraph
from citadel.models.document import Document
from citadel.models.library import Library
from citadel.models.table import Table
from citadel.schemas.content import Block
from citadel.schemas.tree import DocumentTree, TreeNode
from citadel.services import tree


async def get_or_create_library(session: AsyncSession, name: str) -> Library:
    existing = await session.scalar(select(Library).where(Library.name == name))
    if existing is not None:
        return existing
    library = Library(name=name)
    session.add(library)
    await session.flush()
    return library


async def create_document(library: str, filename: str) -> int:
    async with get_sessionmaker()() as session, session.begin():
        lib = await get_or_create_library(session, library)
        document = Document(library_id=lib.id, filename=filename, status="pending")
        session.add(document)
        await session.flush()
        return document.id


def _add_detail(session: AsyncSession, spec: tree.NodeSpec, doc_id: int) -> None:
    match spec.kind:
        case "heading":
            session.add(Heading(content_id=spec.content_id, text=spec.text or ""))
        case "code":
            session.add(Code(content_id=spec.content_id, text=spec.text or ""))
        case "equation":
            session.add(Equation(content_id=spec.content_id, latex=spec.latex or ""))
        case "list":
            session.add(ListBlock(content_id=spec.content_id, items=spec.items or []))
        case "table":
            session.add(
                Table(
                    content_id=spec.content_id,
                    document_id=doc_id,
                    origin="pdf",
                    columns=[],
                    table_metadata={"html": spec.table_html or ""},
                    description="",
                    n_rows=0,
                    sample_rows=[],
                )
            )
        case _:
            session.add(Paragraph(content_id=spec.content_id, text=spec.text or ""))


async def save_document_tree(doc_id: int, blocks: list[Block], status: str) -> None:
    async with get_sessionmaker()() as session, session.begin():
        document = await session.get_one(Document, doc_id)
        document.status = status
        id_map: dict[str, int] = {}
        for spec in tree.build_tree(blocks, document.library_id, doc_id):
            parent_id = id_map[spec.parent_content_id] if spec.parent_content_id is not None else None
            node = ContentNode(
                content_id=spec.content_id,
                document_id=doc_id,
                parent_id=parent_id,
                ordinal=spec.ordinal,
                page_no=spec.page_no,
                type=spec.type,
                level=spec.level,
                bbox=spec.bbox,
            )
            session.add(node)
            await session.flush()
            id_map[spec.content_id] = node.id
            _add_detail(session, spec, doc_id)


async def _document_tree(session: AsyncSession, document: Document) -> DocumentTree:
    nodes = list(
        await session.scalars(
            select(ContentNode)
            .where(ContentNode.document_id == document.id)
            .order_by(ContentNode.page_no, ContentNode.ordinal)
        )
    )
    ids = [node.content_id for node in nodes]
    paragraphs = {
        row.content_id: row.text for row in await session.scalars(select(Paragraph).where(Paragraph.content_id.in_(ids)))
    }
    headings = {
        row.content_id: row.text for row in await session.scalars(select(Heading).where(Heading.content_id.in_(ids)))
    }
    codes = {row.content_id: row.text for row in await session.scalars(select(Code).where(Code.content_id.in_(ids)))}
    equations = {
        row.content_id: row.latex for row in await session.scalars(select(Equation).where(Equation.content_id.in_(ids)))
    }
    lists = {
        row.content_id: row.items for row in await session.scalars(select(ListBlock).where(ListBlock.content_id.in_(ids)))
    }
    tables = {row.content_id: row for row in await session.scalars(select(Table).where(Table.content_id.in_(ids)))}
    by_id: dict[int, TreeNode] = {}
    roots: list[TreeNode] = []
    for node in nodes:
        tnode = TreeNode(type=node.type, level=node.level)
        match tree.detail_kind(node.type):
            case "heading":
                tnode.content = headings.get(node.content_id)
            case "code":
                tnode.content = codes.get(node.content_id)
            case "equation":
                tnode.content = equations.get(node.content_id)
            case "list":
                tnode.list_items = lists.get(node.content_id)
            case "table":
                table = tables.get(node.content_id)
                tnode.content = table.table_metadata.get("html") if table is not None else None
            case _:
                tnode.content = paragraphs.get(node.content_id)
        by_id[node.id] = tnode
        if node.parent_id is None:
            roots.append(tnode)
        else:
            by_id[node.parent_id].children.append(tnode)
    return DocumentTree(filename=document.filename, nodes=roots)


async def get_document_tree(doc_id: int) -> DocumentTree | None:
    async with get_sessionmaker()() as session:
        document = await session.get(Document, doc_id)
        if document is None:
            return None
        return await _document_tree(session, document)


async def library_exists(library_id: int) -> bool:
    async with get_sessionmaker()() as session:
        return await session.get(Library, library_id) is not None


async def iter_library(library_id: int) -> AsyncIterator[DocumentTree]:
    async with get_sessionmaker()() as session:
        documents = list(
            await session.scalars(select(Document).where(Document.library_id == library_id).order_by(Document.id))
        )
        for document in documents:
            yield await _document_tree(session, document)
