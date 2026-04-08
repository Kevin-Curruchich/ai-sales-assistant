from sqlalchemy import create_engine, event, MetaData, text
from sqlalchemy.orm import sessionmaker, DeclarativeBase
from app.core.config import settings

SCHEMA = settings.POSTGRES_SCHEMA

# Create the SQLAlchemy engine
engine = create_engine(
    settings.SQLALCHEMY_DATABASE_URI,
    pool_pre_ping=True, # Verify connections before using them
    echo=False # Set to True to log SQL queries
)

# Set search_path to the desired schema on every new connection
@event.listens_for(engine, "connect")
def set_search_path(dbapi_connection, connection_record):
    cursor = dbapi_connection.cursor()
    cursor.execute(f"SET search_path TO {SCHEMA}")
    cursor.close()
    dbapi_connection.commit()

# Create a configured "Session" class
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

# Base class for declarative models — all tables live in the configured schema
class Base(DeclarativeBase):
    metadata = MetaData(schema=SCHEMA)


def prepare_schema_bootstrap() -> None:
    """Drop stale enum types before create_all so it can recreate them cleanly."""
    with engine.begin() as conn:
        # If the enum exists with wrong-case labels (e.g. PERCENT/FEE from old runs),
        # drop it so create_all can recreate it with correct lowercase values.
        conn.execute(
            text(
                "DO $$ BEGIN "
                f"IF EXISTS ("
                f"SELECT 1 FROM pg_enum e "
                f"JOIN pg_type t ON t.oid = e.enumtypid "
                f"JOIN pg_namespace n ON n.oid = t.typnamespace "
                f"WHERE t.typname = 'earning_mode_enum' "
                f"AND n.nspname = '{SCHEMA}' "
                f"AND e.enumlabel NOT IN ('percent', 'fee')"
                f") THEN "
                f"DROP TYPE {SCHEMA}.earning_mode_enum CASCADE; "
                "END IF; END $$;"
            )
        )
        # Also drop from public if it was accidentally created there.
        conn.execute(text("DROP TYPE IF EXISTS public.earning_mode_enum CASCADE"))


def ensure_schema_compatibility() -> None:
    """Apply additive schema updates for running environments without Alembic."""
    with engine.begin() as conn:
        conn.execute(
            text(
                "DO $$ BEGIN "
                "IF NOT EXISTS ("
                "SELECT 1 FROM information_schema.columns "
                f"WHERE table_schema = '{SCHEMA}' AND table_name = 'purchase_items' AND column_name = 'remaining_quantity'"
                ") THEN "
                f"ALTER TABLE {SCHEMA}.purchase_items ADD COLUMN remaining_quantity INTEGER NOT NULL DEFAULT 0; "
                f"UPDATE {SCHEMA}.purchase_items pi "
                f"SET remaining_quantity = pi.quantity "
                f"FROM {SCHEMA}.purchases p "
                "WHERE p.id = pi.purchase_id AND p.status = 'confirmed'; "
                "END IF; "
                "END $$;"
            )
        )

        conn.execute(
            text(
                f"ALTER TABLE {SCHEMA}.sale_items "
                "ADD COLUMN IF NOT EXISTS discount_percent NUMERIC(5, 2), "
                "ADD COLUMN IF NOT EXISTS discount_amount NUMERIC(14, 2);"
            )
        )



