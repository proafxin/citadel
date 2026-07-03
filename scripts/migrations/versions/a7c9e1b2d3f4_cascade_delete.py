from collections.abc import Sequence

from alembic import op

revision: str = "a7c9e1b2d3f4"
down_revision: str | Sequence[str] | None = "f1e2d3c4b5a6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

FKS = [
    ("documents", "documents_library_id_fkey", ["library_id"], "libraries", ["id"]),
    ("content", "content_document_id_fkey", ["document_id"], "documents", ["id"]),
    ("content", "content_parent_id_fkey", ["parent_id"], "content", ["id"]),
    ("codes", "codes_content_id_fkey", ["content_id"], "content", ["content_id"]),
    ("equations", "equations_content_id_fkey", ["content_id"], "content", ["content_id"]),
    ("lists", "lists_content_id_fkey", ["content_id"], "content", ["content_id"]),
    ("paragraphs", "paragraphs_content_id_fkey", ["content_id"], "content", ["content_id"]),
    ("tables", "tables_content_id_fkey", ["content_id"], "content", ["content_id"]),
    ("tables", "tables_document_id_fkey", ["document_id"], "documents", ["id"]),
    ("table_rows", "table_rows_table_id_fkey", ["table_id"], "tables", ["id"]),
]


def upgrade() -> None:
    for table, name, cols, ref_table, ref_cols in FKS:
        op.drop_constraint(name, table, type_="foreignkey")
        op.create_foreign_key(name, table, ref_table, cols, ref_cols, ondelete="CASCADE")


def downgrade() -> None:
    for table, name, cols, ref_table, ref_cols in FKS:
        op.drop_constraint(name, table, type_="foreignkey")
        op.create_foreign_key(name, table, ref_table, cols, ref_cols)
