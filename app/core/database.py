import uuid

from sqlalchemy import MetaData, create_engine, event, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.core.config import settings

SCHEMA = settings.POSTGRES_SCHEMA

# Create the SQLAlchemy engine
engine = create_engine(
    settings.SQLALCHEMY_DATABASE_URI,
    pool_pre_ping=True,  # Verify connections before using them
    echo=False,  # Set to True to log SQL queries
)


# Set search_path to the desired schema on every new connection
@event.listens_for(engine, "connect")
def set_search_path(dbapi_connection, connection_record):
    cursor = dbapi_connection.cursor()
    cursor.execute(f"SET search_path TO {SCHEMA}")
    cursor.close()
    dbapi_connection.commit()


# Contexto de comercio para RLS: la variable es local a la transaccion, asi que
# se reaplica en cada after_begin.  Escucha la clase Session (no SessionLocal)
# para cubrir tambien las sesiones de tests y del worker.
def _apply_business(connection_owner, business_id) -> None:
    connection_owner.execute(
        text("select set_config('app.business_id', :id, true)"),
        {"id": str(business_id)},
    )


@event.listens_for(Session, "after_begin")
def _set_business_on_begin(session, transaction, connection):
    business_id = session.info.get("business_id")
    if business_id is not None:
        _apply_business(connection, business_id)


def bind_business(db: Session, business_id: uuid.UUID, timezone: str) -> None:
    db.info["business_id"] = business_id
    db.info["business_tz"] = timezone
    if db.in_transaction():
        _apply_business(db, business_id)


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
