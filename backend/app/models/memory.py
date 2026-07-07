import uuid
from datetime import datetime, UTC
from sqlalchemy import String, Text, Float, DateTime, JSON, ForeignKey, Index
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import TypeDecorator, UserDefinedType
from app.database import Base


class PGVector(UserDefinedType):
    """Minimal pgvector `vector(N)` type using text-format binds.

    asyncpg has no codec for the vector OID, but it encodes str parameters
    as text and PostgreSQL casts them; a plain JSON column type instead
    renders `$n::JSON` and every INSERT fails with DatatypeMismatchError.
    """
    cache_ok = True

    def __init__(self, dim: int):
        self.dim = dim

    def get_col_spec(self, **kw) -> str:
        return f"vector({self.dim})"

    def bind_processor(self, dialect):
        def process(value):
            if value is None:
                return None
            return "[" + ",".join(repr(float(v)) for v in value) + "]"
        return process

    def result_processor(self, dialect, coltype):
        def process(value):
            if value is None or not isinstance(value, str):
                return value
            body = value.strip()[1:-1]
            return [float(x) for x in body.split(",")] if body else []
        return process


class EmbeddingVector(TypeDecorator):
    """vector(1024) on PostgreSQL (migration 004), JSON in sqlite dev/tests."""
    impl = JSON
    cache_ok = True

    def load_dialect_impl(self, dialect):
        if dialect.name == "postgresql":
            return dialect.type_descriptor(PGVector(1024))
        # none_as_null: Python None must land as SQL NULL, not JSON 'null'
        # text, so the `embedding IS NOT NULL` filter works (P0-5)
        return dialect.type_descriptor(JSON(none_as_null=True))


class Memory(Base):
    __tablename__ = "memories"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    resident_id: Mapped[str] = mapped_column(String, ForeignKey("residents.id"), index=True)
    type: Mapped[str] = mapped_column(String(20))  # "event", "relationship", "reflection"
    content: Mapped[str] = mapped_column(Text)
    importance: Mapped[float] = mapped_column(Float, default=0.5)
    source: Mapped[str] = mapped_column(String(20))  # "chat_player", "chat_resident", "observation", "reflection", "media"

    # Relationship pointers (nullable)
    related_resident_id: Mapped[str | None] = mapped_column(String, ForeignKey("residents.id"), nullable=True)
    related_user_id: Mapped[str | None] = mapped_column(String, ForeignKey("users.id"), nullable=True)

    # Media (nullable, for P2 multimodal)
    media_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    media_summary: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Embedding (nullable — only event memories get embeddings)
    embedding: Mapped[list | None] = mapped_column(EmbeddingVector, nullable=True)

    metadata_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))
    last_accessed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))

    __table_args__ = (
        Index("ix_memories_resident_type", "resident_id", "type"),
        Index("ix_memories_resident_related_resident", "resident_id", "related_resident_id"),
        Index("ix_memories_resident_related_user", "resident_id", "related_user_id"),
    )
