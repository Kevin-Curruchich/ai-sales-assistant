import uuid
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker

from app.models import Customer, Product, Sale, SaleItem, User
from scripts.copy_schema_data import (
    BORN_EMPTY_TABLES,
    SchemaMismatch,
    TABLE_ORDER,
    copy_schema_data,
)
from tests.conftest import TEST_DATABASE_URL


def _session_for(schema: str):
    engine = create_engine(TEST_DATABASE_URL)

    @event.listens_for(engine, "connect")
    def _set_search_path(dbapi_connection, _record):
        cursor = dbapi_connection.cursor()
        cursor.execute(f'SET search_path TO "{schema}"')
        cursor.close()
        dbapi_connection.commit()

    return sessionmaker(bind=engine)()


def _seed(schema: str) -> None:
    session = _session_for(schema)
    user = User(email=f"{uuid.uuid4().hex}@example.com", role="admin")
    customer = Customer(name="Aurita")
    product = Product(sku=f"SKU-{uuid.uuid4().hex[:6]}", name="Cartón de huevos")
    session.add_all([user, customer, product])
    session.flush()

    sale = Sale(
        customer_id=customer.id,
        user_id=user.id,
        date=date(2026, 8, 5),
        total=Decimal("18.00"),
    )
    session.add(sale)
    session.flush()

    session.add(
        SaleItem(
            sale_id=sale.id,
            product_id=product.id,
            quantity=Decimal("0.5"),
            unit_price=Decimal("36.00"),
            subtotal=Decimal("18.00"),
        )
    )
    session.commit()
    session.close()


def test_table_order_respects_foreign_keys():
    assert TABLE_ORDER.index("users") < TABLE_ORDER.index("sales")
    assert TABLE_ORDER.index("customers") < TABLE_ORDER.index("sales")
    assert TABLE_ORDER.index("sales") < TABLE_ORDER.index("sale_items")
    assert TABLE_ORDER.index("purchases") < TABLE_ORDER.index("purchase_items")
    assert TABLE_ORDER.index("purchase_items") < TABLE_ORDER.index(
        "sale_item_lot_allocations"
    )


def test_copy_preserves_row_counts_and_sums(
    test_engine, alembic_config, second_migrated_schema
):
    from alembic import command

    command.upgrade(alembic_config, "head")
    source = alembic_config.attributes["target_schema"]
    target = second_migrated_schema
    _seed(source)

    copied = copy_schema_data(test_engine, source, target)

    assert copied["sales"] == 1
    assert copied["sale_items"] == 1
    with test_engine.connect() as conn:
        conn.execute(text(f'SET search_path TO "{target}"'))
        assert conn.execute(text("SELECT count(*) FROM sales")).scalar() == 1
        assert conn.execute(text("SELECT sum(total) FROM sales")).scalar() == Decimal(
            "18.00"
        )
        assert conn.execute(
            text("SELECT sum(quantity) FROM sale_items")
        ).scalar() == Decimal("0.5000")


def test_copy_aborts_when_target_lacks_a_column(
    test_engine, alembic_config, second_migrated_schema
):
    from alembic import command

    command.upgrade(alembic_config, "head")
    source = alembic_config.attributes["target_schema"]
    target = second_migrated_schema

    with test_engine.begin() as conn:
        conn.execute(text(f'ALTER TABLE "{source}".customers ADD COLUMN nickname TEXT'))

    with pytest.raises(SchemaMismatch, match="nickname"):
        copy_schema_data(test_engine, source, target)


def test_dry_run_copies_nothing(test_engine, alembic_config, second_migrated_schema):
    from alembic import command

    command.upgrade(alembic_config, "head")
    source = alembic_config.attributes["target_schema"]
    target = second_migrated_schema
    _seed(source)

    copy_schema_data(test_engine, source, target, dry_run=True)

    with test_engine.connect() as conn:
        conn.execute(text(f'SET search_path TO "{target}"'))
        assert conn.execute(text("SELECT count(*) FROM sales")).scalar() == 0


def test_copy_aborts_when_target_narrows_a_numeric_scale(
    test_engine, alembic_config, second_migrated_schema
):
    """Un destino con menos escala redondea plata en silencio si no se detecta.

    sale_items.quantity es numeric(10,4) en el origen. Si el destino lo
    tuviera como numeric(10,2), un INSERT ... SELECT sin control de tipos
    no falla: Postgres redondea 0.5000 a 0.50 en el momento de la escritura,
    sin excepcion y sin warning. copy_schema_data debe detectar esta
    diferencia comparando tipos (no solo nombres de columna) y abortar antes
    de escribir nada.
    """
    from alembic import command

    command.upgrade(alembic_config, "head")
    source = alembic_config.attributes["target_schema"]
    target = second_migrated_schema
    _seed(source)

    with test_engine.begin() as conn:
        conn.execute(
            text(
                f'ALTER TABLE "{target}".sale_items '
                "ALTER COLUMN quantity TYPE numeric(10,2)"
            )
        )

    with pytest.raises(SchemaMismatch, match="quantity"):
        copy_schema_data(test_engine, source, target)

    with test_engine.connect() as conn:
        conn.execute(text(f'SET search_path TO "{target}"'))
        assert conn.execute(text("SELECT count(*) FROM sales")).scalar() == 0
        assert conn.execute(text("SELECT count(*) FROM sale_items")).scalar() == 0


def test_cash_movements_is_exempt_only_while_it_is_empty(
    test_engine, alembic_config, second_migrated_schema
):
    """La exencion de BORN_EMPTY_TABLES no puede ser un cheque en blanco.

    cash_movements no esta en TABLE_ORDER: nace vacia y la Task 7 no importa la
    pestania del Sheet, asi que una copia normal debe funcionar pese a que la
    tabla exista en ambos schemas.  Pero en cuanto el origen tenga movimientos
    reales, saltarsela seria una copia incompleta y silenciosa, y el guard tiene
    que volver a morder.
    """
    from alembic import command

    command.upgrade(alembic_config, "head")
    source = alembic_config.attributes["target_schema"]
    target = second_migrated_schema

    assert "cash_movements" in BORN_EMPTY_TABLES
    assert "cash_movements" not in TABLE_ORDER

    # Vacia: la copia procede sin quejarse.
    copy_schema_data(test_engine, source, target)

    # Con una fila: la copia aborta nombrando la tabla.
    with test_engine.begin() as conn:
        conn.execute(text(f'SET search_path TO "{source}"'))
        conn.execute(
            text(
                "INSERT INTO cash_movements (occurred_at, type, amount) "
                "VALUES (:d, CAST(:t AS cash_movement_type_enum), :a)"
            ),
            {"d": datetime(2026, 9, 3, 9, 0, tzinfo=timezone.utc), "t": "entrada", "a": Decimal("115.00")},
        )

    with pytest.raises(SchemaMismatch, match="cash_movements"):
        copy_schema_data(test_engine, source, target)


def test_copy_aborts_when_source_has_a_table_outside_table_order(
    test_engine, alembic_config, second_migrated_schema
):
    """Una tabla del origen que TABLE_ORDER no conoce no debe copiarse en silencio."""
    from alembic import command

    command.upgrade(alembic_config, "head")
    source = alembic_config.attributes["target_schema"]
    target = second_migrated_schema

    with test_engine.begin() as conn:
        conn.execute(text(f'CREATE TABLE "{source}".mystery (id serial primary key)'))

    with pytest.raises(SchemaMismatch, match="mystery"):
        copy_schema_data(test_engine, source, target)


def test_copy_rolls_back_completely_when_a_late_table_mismatches(
    test_engine, alembic_config, second_migrated_schema
):
    """La propiedad central: todo o nada, incluso si el fallo ocurre tarde.

    sale_items es la septima tabla de nueve en TABLE_ORDER: para cuando el
    mismatch se detecta ahi, users/customers/products/purchases/
    purchase_items/sales ya se habrian insertado en el destino de no haber
    control transaccional explicito. Si alguien reemplazara el
    engine.connect() + conn.begin() de copy_schema_data por, por ejemplo,
    ejecutar cada INSERT en su propia transaccion implicita, este test lo
    detectaria: las tablas tempranas quedarian con filas aunque la copia
    completa haya fallado.
    """
    from alembic import command

    command.upgrade(alembic_config, "head")
    source = alembic_config.attributes["target_schema"]
    target = second_migrated_schema
    _seed(source)

    with test_engine.begin() as conn:
        conn.execute(text(f'ALTER TABLE "{source}".sale_items ADD COLUMN extra_col TEXT'))

    with pytest.raises(SchemaMismatch):
        copy_schema_data(test_engine, source, target)

    with test_engine.connect() as conn:
        conn.execute(text(f'SET search_path TO "{target}"'))
        for table in ("users", "customers", "products", "sales"):
            count = conn.execute(text(f'SELECT count(*) FROM "{table}"')).scalar()
            assert count == 0, f"{table} deberia seguir vacia tras el rollback, tiene {count}"


def test_copy_refuses_db_dev_as_target_without_explicit_opt_in(
    test_engine, alembic_config, monkeypatch
):
    from alembic import command

    command.upgrade(alembic_config, "head")
    source = alembic_config.attributes["target_schema"]

    monkeypatch.delenv("REVENEW_ALLOW_DB_DEV", raising=False)

    with pytest.raises(RuntimeError, match="db_dev"):
        copy_schema_data(test_engine, source, "db_dev")


def test_copy_allows_db_dev_target_with_explicit_opt_in(
    test_engine, alembic_config, monkeypatch
):
    """El opt-in debe saltear el guard, no desactivar el resto de las validaciones."""
    from alembic import command

    command.upgrade(alembic_config, "head")
    source = alembic_config.attributes["target_schema"]

    monkeypatch.setenv("REVENEW_ALLOW_DB_DEV", "1")

    # No existe un schema "db_dev" en la base de test: si el guard se salteo
    # de verdad, la falla siguiente es la validacion normal de "no existe",
    # no el RuntimeError del guard.
    with pytest.raises(SchemaMismatch, match="no existe"):
        copy_schema_data(test_engine, source, "db_dev")
