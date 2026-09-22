from pathlib import Path

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import text

from app.core.database import Base
import app.models  # noqa: F401

EXPECTED_TABLES = {
    "users", "customers", "products", "purchases", "purchase_items",
    "sales", "sale_items", "sale_item_lot_allocations", "customer_product_cycles",
}


def _tables_in(conn, schema: str) -> set[str]:
    rows = conn.execute(
        text("SELECT tablename FROM pg_tables WHERE schemaname = :s"), {"s": schema}
    )
    return {r[0] for r in rows}


def test_upgrade_head_creates_every_table(test_engine, migrated_schema):
    with test_engine.connect() as conn:
        tables = _tables_in(conn, migrated_schema)
    assert EXPECTED_TABLES <= tables


def test_models_and_migrations_do_not_drift(test_engine, migrated_schema):
    """El test mas importante: tras migrar, autogenerate no debe detectar nada."""
    with test_engine.connect() as conn:
        conn.execute(text(f'SET search_path TO "{migrated_schema}"'))
        ctx = MigrationContext.configure(
            conn,
            opts={
                "compare_type": True,
                "include_schemas": False,
                # version_table_schema debe ir en None, no en migrated_schema.  Con
                # include_schemas=False el autogenerate refleja el schema por
                # defecto de la conexion (que el SET search_path de arriba ya fijo
                # en migrated_schema) y lo identifica como None; alembic excluye su
                # propia tabla de version comparando (schema, nombre) literalmente
                # (autogenerate/compare.py, _autogen_for_tables), asi que pasarle el
                # nombre real hace que no reconozca alembic_version y la reporte
                # como "removed table".  Ese falso positivo taparia drift real.
                "version_table_schema": None,
            },
        )
        diff = compare_metadata(ctx, Base.metadata)
    assert diff == [], f"Los modelos y las migraciones divergen: {diff}"


def test_downgrade_removes_everything(test_engine, alembic_config):
    schema = alembic_config.attributes["target_schema"]
    command.upgrade(alembic_config, "head")
    command.downgrade(alembic_config, "base")

    with test_engine.connect() as conn:
        tables = _tables_in(conn, schema)
    assert EXPECTED_TABLES & tables == set(), f"Quedaron tablas tras downgrade: {tables}"


def _normalized_defaults(conn, schema: str) -> dict[tuple[str, str], str]:
    """Defaults del catalogo, normalizados para no atarse al formato de Postgres.

    Postgres reescribe lo que le mandamos: `sa.text("'percent'")` sobre una columna
    enum vuelve como `'percent'::<schema>.earning_mode_enum`, con el nombre del
    schema desechable incrustado.  Comparar el texto crudo haria el test fragil y
    dependiente del nombre aleatorio del schema, asi que se recorta el cast y las
    comillas y se compara el valor, no su serializacion.
    """
    rows = conn.execute(
        text(
            "SELECT table_name, column_name, column_default "
            "FROM information_schema.columns "
            "WHERE table_schema = :s AND column_default IS NOT NULL"
        ),
        {"s": schema},
    )
    out = {}
    for table, column, default in rows:
        value = default.split("::")[0].strip().strip("'").lower()
        out[(table, column)] = value
    return out


def test_server_defaults_survive_the_migration(test_engine, migrated_schema):
    """Los server defaults que importan, asertados contra el catalogo.

    compare_metadata corre con compare_server_default=False (activarlo da falsos
    positivos en Postgres: compara el texto del default, y `'0'::numeric` vs `0`
    diverge), asi que test_models_and_migrations_do_not_drift es CIEGO a esta clase
    de fallo.  Es justo la que ya mordio una vez: la migracion nacio sin el
    DEFAULT 0 de purchase_items.remaining_quantity y ningun test lo noto.

    El brief avisa ademas de que el autogenerate "a veces pierde" los
    server_default, y los nueve gen_random_uuid() de las PK son lo mas caro de
    perder: sin ellos, cualquier INSERT que no traiga el id explicito falla.
    """
    with test_engine.connect() as conn:
        defaults = _normalized_defaults(conn, migrated_schema)

    missing_uuid_defaults = sorted(
        table
        for table in EXPECTED_TABLES
        if defaults.get((table, "id")) != "gen_random_uuid()"
    )
    assert missing_uuid_defaults == [], (
        "Estas tablas perdieron el server default de su PK UUID: "
        f"{missing_uuid_defaults}.  Un INSERT sin id explicito fallaria."
    )

    # Las tres columnas que ensure_schema_compatibility() parcheaba en caliente y
    # que la copia de datos de la Task 6 espera encontrar tal cual en produccion.
    assert defaults.get(("purchase_items", "remaining_quantity")) == "0"
    assert defaults.get(("sales", "is_payment_pending")) == "false"
    assert defaults.get(("products", "earning_mode")) == "percent"


def test_upgrade_downgrade_upgrade_round_trip(test_engine, alembic_config):
    """La reversibilidad tiene que ser real: bajar y volver a subir el MISMO schema.

    test_downgrade_removes_everything solo va en una direccion, y cada test recibe
    un schema desechable que se destruye con CASCADE, asi que ninguno de los dos
    nota si el downgrade se deja algo que no sea una tabla.  El caso concreto es el
    enum nativo earning_mode_enum: op.create_table emite su CREATE TYPE, pero
    op.drop_table no emite el DROP TYPE.  Sin el drop explicito del downgrade, este
    segundo upgrade revienta con 'type "earning_mode_enum" already exists'.
    """
    schema = alembic_config.attributes["target_schema"]

    command.upgrade(alembic_config, "head")
    command.downgrade(alembic_config, "base")

    with test_engine.connect() as conn:
        leftover_types = conn.execute(
            text(
                "SELECT t.typname FROM pg_type t "
                "JOIN pg_namespace n ON n.oid = t.typnamespace "
                "WHERE n.nspname = :s AND t.typtype = 'e'"
            ),
            {"s": schema},
        ).fetchall()
    assert leftover_types == [], (
        f"El downgrade dejo tipos sin borrar en {schema}: {leftover_types}. "
        "El siguiente upgrade fallaria."
    )

    command.upgrade(alembic_config, "head")

    with test_engine.connect() as conn:
        tables = _tables_in(conn, schema)
    assert EXPECTED_TABLES <= tables


def test_migration_emits_no_hardcoded_schema():
    """Ninguna revision puede cualificar el schema: se resuelve por search_path.

    Se comprueba `schema=` en general, no el literal 'db_dev'.  El contrato no es
    "no apuntes a db_dev", es "no cualifiques el schema en absoluto": un
    schema='db_v2' o un schema='public' colado romperia db_dev igual de bien.

    El directorio se resuelve desde __file__, no desde el cwd: con Path relativa,
    correr pytest desde cualquier sitio que no sea la raiz del repo hacia que el
    glob no encontrase nada y el test pasara sin mirar nada.  Por eso se asserta
    tambien que se escaneo al menos una revision.
    """
    versions_dir = Path(__file__).resolve().parent.parent / "alembic" / "versions"
    revisions = sorted(versions_dir.glob("*.py"))

    assert revisions, (
        f"No se escaneo ninguna revision en {versions_dir}.  El test no puede dar "
        "por bueno lo que no ha leido."
    )

    for path in revisions:
        source = path.read_text()
        assert "schema=" not in source, (
            f"{path} cualifica el schema explicitamente.  Estas migraciones tienen "
            "que servir para db_dev, db_v2 y cualquier schema de test, y el schema "
            "lo fija el search_path de env.py."
        )
