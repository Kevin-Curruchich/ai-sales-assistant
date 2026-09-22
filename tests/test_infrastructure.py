from sqlalchemy import text


def test_throwaway_schema_exists_and_is_empty(test_engine, throwaway_schema):
    with test_engine.connect() as conn:
        exists = conn.execute(
            text("SELECT 1 FROM pg_namespace WHERE nspname = :s"),
            {"s": throwaway_schema},
        ).scalar()
        tables = conn.execute(
            text("SELECT count(*) FROM pg_tables WHERE schemaname = :s"),
            {"s": throwaway_schema},
        ).scalar()
    assert exists == 1
    assert tables == 0
