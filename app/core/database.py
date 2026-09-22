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

# Convencion de nombres para constraints e indices.  Sin ella, Postgres asigna
# nombres por defecto que el autogenerate de Alembic no puede alterar ni borrar
# de forma fiable mas adelante.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


# Base declarativa.  El metadata NO lleva schema: las tablas se resuelven por el
# search_path que fija set_search_path() en cada conexion, de modo que el mismo
# modelo y las mismas migraciones sirven para db_dev, db_v2, public o un schema
# de test.
class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


