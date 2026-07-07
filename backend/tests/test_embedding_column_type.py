"""Memory.embedding must compile to vector(1024) on PG and JSON on sqlite.

Migration 004 creates the column as pgvector `vector(1024)`; a plain JSON
ORM type makes asyncpg render `$n::JSON` and every INSERT fails with
DatatypeMismatchError — even for NULL embeddings (seen live on vm212).
"""
from sqlalchemy.dialects import postgresql, sqlite

from app.models.memory import Memory


def test_embedding_renders_vector_on_postgresql():
    col_type = Memory.__table__.c.embedding.type
    impl = col_type.load_dialect_impl(postgresql.dialect())
    assert impl.__class__.__name__ == "PGVector", f"got {impl.__class__.__name__}"
    assert "vector(1024)" in impl.get_col_spec()
    # DDL compile must render the pgvector type, not JSON/JSONB
    ddl = Memory.__table__.c.embedding.type.compile(dialect=postgresql.dialect())
    assert ddl == "vector(1024)", ddl


def test_embedding_stays_json_on_sqlite():
    col_type = Memory.__table__.c.embedding.type
    impl = col_type.load_dialect_impl(sqlite.dialect())
    assert "JSON" in impl.__class__.__name__.upper()


def test_pg_bind_and_result_roundtrip_text_format():
    from app.models.memory import PGVector

    t = PGVector(1024)
    bind = t.bind_processor(None)
    assert bind(None) is None
    assert bind([0.1, 0.2]) == "[0.1,0.2]"
    result = t.result_processor(None, None)
    assert result(None) is None
    assert result("[0.1,0.2]") == [0.1, 0.2]
