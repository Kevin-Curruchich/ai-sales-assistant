from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.models.payment_method import PaymentMethod


def test_payment_method_values():
    assert {m.value for m in PaymentMethod} == {"efectivo", "transferencia"}


def test_columns_are_varchar_50_not_a_native_enum(test_engine, migrated_schema):
    """Un enum nativo haria que agregar un medio de pago pelee con Alembic."""
    with test_engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT table_name, data_type, character_maximum_length "
                "FROM information_schema.columns "
                "WHERE table_schema = :s AND column_name = 'payment_method'"
            ),
            {"s": migrated_schema},
        ).all()
    assert {r[0] for r in rows} == {"sales", "purchases", "cash_movements"}
    for _, data_type, length in rows:
        assert data_type == "character varying"
        assert length == 50


def test_check_accepts_the_two_values_and_null(test_engine, migrated_schema):
    """NULL debe pasar: hoy las 194 filas de produccion lo tienen en NULL."""
    with test_engine.begin() as conn:
        conn.execute(text(f'SET search_path TO "{migrated_schema}"'))
        for value in ("'efectivo'", "'transferencia'", "NULL"):
            conn.execute(
                text(
                    "INSERT INTO cash_movements (movement_date, type, amount, payment_method) "
                    f"VALUES ('2026-09-23', CAST('entrada' AS cash_movement_type_enum), 10, {value})"
                )
            )
        total = conn.execute(text("SELECT count(*) FROM cash_movements")).scalar()
    assert total == 3


def test_check_rejects_anything_else(test_engine, migrated_schema):
    with test_engine.begin() as conn:
        conn.execute(text(f'SET search_path TO "{migrated_schema}"'))
        with pytest.raises(IntegrityError):
            conn.execute(
                text(
                    "INSERT INTO cash_movements (movement_date, type, amount, payment_method) "
                    "VALUES ('2026-09-23', CAST('entrada' AS cash_movement_type_enum), 10, 'Efectivo')"
                )
            )
