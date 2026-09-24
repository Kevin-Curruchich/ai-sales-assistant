import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.core.database import Base
from app.models import CashMovement, CashMovementType
import app.models  # noqa: F401


def test_cash_movement_type_values():
    assert {m.value for m in CashMovementType} == {
        "entrada",
        "salida",
        "aporte_socio",
        "retiro_socio",
        "saldo_inicial",
    }


def test_new_payment_fields_exist_on_the_models():
    assert "payment_date" in Base.metadata.tables["sales"].c
    assert "payment_method" in Base.metadata.tables["sales"].c
    assert "payment_method" in Base.metadata.tables["purchases"].c
    assert "expected_unit_margin" in Base.metadata.tables["sale_items"].c
    cycles = Base.metadata.tables["customer_product_cycles"].c
    assert "projection_method" in cycles
    assert "projection_confidence" in cycles
    assert "calendar_event_id" in cycles


def test_payment_date_is_nullable():
    """VTA-046 del Sheet esta pagada sin fecha de pago: no puede ser obligatoria."""
    assert Base.metadata.tables["sales"].c.payment_date.nullable is True


def test_cash_movements_table_has_both_nullable_links(test_engine, migrated_schema):
    with test_engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT column_name, is_nullable FROM information_schema.columns "
                "WHERE table_schema = :s AND table_name = 'cash_movements'"
            ),
            {"s": migrated_schema},
        )
        nullable = {r[0]: r[1] for r in rows}
    assert nullable["sale_id"] == "YES"
    assert nullable["purchase_id"] == "YES"
    assert nullable["amount"] == "NO"


def test_running_balance_is_not_stored(test_engine, migrated_schema):
    """El saldo acumulado se calcula al leer; en el Sheet la columna se desincronizo."""
    with test_engine.connect() as conn:
        cols = {
            r[0]
            for r in conn.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema = :s AND table_name = 'cash_movements'"
                ),
                {"s": migrated_schema},
            )
        }
    assert "running_balance" not in cols
    assert "saldo_acumulado" not in cols


def test_saldo_inicial_is_accepted_by_the_check_constraint(test_engine, migrated_schema):
    with test_engine.begin() as conn:
        conn.execute(text(f'SET search_path TO "{migrated_schema}"'))
        conn.execute(
            text(
                "INSERT INTO cash_movements (id, occurred_at, type, amount) "
                "VALUES (gen_random_uuid(), now(), 'saldo_inicial', 500.00)"
            )
        )
        count = conn.execute(
            text("SELECT count(*) FROM cash_movements WHERE type = 'saldo_inicial'")
        ).scalar()
    assert count == 1


def test_cash_movement_type_column_is_varchar_not_a_native_enum(test_engine, migrated_schema):
    """Mismo patron que payment_method (tests/test_payment_method.py): un enum
    nativo es schema-scoped (db_v2.cash_movement_type_enum != db_local...) y
    ademas pelea con el DDL transaccional de Alembic para agregar valores."""
    with test_engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT data_type, character_maximum_length FROM information_schema.columns "
                "WHERE table_schema = :s AND table_name = 'cash_movements' AND column_name = 'type'"
            ),
            {"s": migrated_schema},
        ).one()
    assert row[0] == "character varying"
    assert row[1] == 20


def test_cash_movement_type_check_accepts_the_five_values(test_engine, migrated_schema):
    with test_engine.begin() as conn:
        conn.execute(text(f'SET search_path TO "{migrated_schema}"'))
        for value in (
            "entrada",
            "salida",
            "aporte_socio",
            "retiro_socio",
            "saldo_inicial",
        ):
            conn.execute(
                text(
                    "INSERT INTO cash_movements (occurred_at, type, amount) "
                    f"VALUES ('2026-09-23T09:00:00Z', '{value}', 10)"
                )
            )
        total = conn.execute(text("SELECT count(*) FROM cash_movements")).scalar()
    assert total == 5


def test_cash_movement_type_check_rejects_anything_else(test_engine, migrated_schema):
    with test_engine.begin() as conn:
        conn.execute(text(f'SET search_path TO "{migrated_schema}"'))
        with pytest.raises(IntegrityError):
            conn.execute(
                text(
                    "INSERT INTO cash_movements (occurred_at, type, amount) "
                    "VALUES ('2026-09-23T09:00:00Z', 'bogus', 10)"
                )
            )


def test_cash_movement_type_is_not_nullable(test_engine, migrated_schema):
    with test_engine.connect() as conn:
        nullable = conn.execute(
            text(
                "SELECT is_nullable FROM information_schema.columns "
                "WHERE table_schema = :s AND table_name = 'cash_movements' AND column_name = 'type'"
            ),
            {"s": migrated_schema},
        ).scalar()
    assert nullable == "NO"
