from datetime import date
from decimal import Decimal

from sqlalchemy import text

from app.core.database import Base
from app.models import CashMovement, CashMovementType
import app.models  # noqa: F401


def test_cash_movement_type_values():
    assert {m.value for m in CashMovementType} == {
        "entrada",
        "salida",
        "aporte_socio",
        "retiro_socio",
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


def test_running_balance_can_be_computed(test_engine, migrated_schema):
    """SUM() OVER reproduce el saldo sin guardarlo."""
    with test_engine.begin() as conn:
        conn.execute(text(f'SET search_path TO "{migrated_schema}"'))
        for day, kind, amount in [
            (date(2026, 9, 3), "entrada", "115.00"),
            (date(2026, 9, 3), "entrada", "37.00"),
            (date(2026, 9, 5), "salida", "285.00"),
        ]:
            conn.execute(
                text(
                    "INSERT INTO cash_movements (movement_date, type, amount) "
                    "VALUES (:d, CAST(:t AS cash_movement_type_enum), :a)"
                ),
                {"d": day, "t": kind, "a": Decimal(amount)},
            )

    with test_engine.connect() as conn:
        conn.execute(text(f'SET search_path TO "{migrated_schema}"'))
        balance = conn.execute(
            text(
                "SELECT SUM(CASE WHEN type = 'salida' THEN -amount ELSE amount END) "
                "OVER (ORDER BY movement_date, amount) "
                "FROM cash_movements ORDER BY movement_date, amount"
            )
        ).scalars().all()
    assert balance[-1] == Decimal("-133.00")
