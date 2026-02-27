from sqlalchemy import create_engine, event, MetaData
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
