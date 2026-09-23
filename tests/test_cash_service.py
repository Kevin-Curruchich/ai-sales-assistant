import uuid
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app.models import CashMovementType
from app.services.cash_service import CashService
from tests.conftest import TEST_DATABASE_URL


def _session_for(schema: str):
    engine = create_engine(TEST_DATABASE_URL)

    @event.listens_for(engine, "connect")
    def _set_search_path(dbapi_connection, _record):
        cursor = dbapi_connection.cursor()
        cursor.execute(f'SET search_path TO "{schema}"')
        cursor.close()
        dbapi_connection.commit()

    return sessionmaker(bind=engine)(), engine


@pytest.fixture
def cash(migrated_schema):
    session, engine = _session_for(migrated_schema)
    try:
        yield CashService(session)
    finally:
        session.close()
        engine.dispose()


def test_running_balance_adds_entries_and_subtracts_exits(cash):
    cash.record(datetime(2026, 9, 3, 9, 0, tzinfo=timezone.utc), CashMovementType.ENTRADA, Decimal("115.00"))
    cash.record(datetime(2026, 9, 3, 10, 0, tzinfo=timezone.utc), CashMovementType.ENTRADA, Decimal("37.00"))
    cash.record(datetime(2026, 9, 5, 9, 0, tzinfo=timezone.utc), CashMovementType.SALIDA, Decimal("285.00"))
    assert cash.running_balance() == Decimal("-133.00")


def test_owner_balance_is_contributions_minus_withdrawals(cash):
    cash.record(datetime(2026, 9, 5, 9, 0, tzinfo=timezone.utc), CashMovementType.APORTE_SOCIO, Decimal("95.00"))
    cash.record(datetime(2026, 9, 9, 9, 0, tzinfo=timezone.utc), CashMovementType.APORTE_SOCIO, Decimal("125.02"))
    cash.record(datetime(2026, 9, 20, 9, 0, tzinfo=timezone.utc), CashMovementType.RETIRO_SOCIO, Decimal("100.00"))
    assert cash.owner_balance() == Decimal("120.02")


def test_partner_contribution_raises_the_business_balance(cash):
    """aporte_socio es dinero que entra a la caja del negocio."""
    cash.record(datetime(2026, 9, 5, 9, 0, tzinfo=timezone.utc), CashMovementType.APORTE_SOCIO, Decimal("95.00"))
    assert cash.running_balance() == Decimal("95.00")


def test_ledger_running_balance_is_stable_with_identical_instants(cash):
    """Tres movimientos con el MISMO instante deben dar un acumulado estable.

    Con occurred_at los empates son raros, pero una carga en lote los produce.
    Sin desempate por created_at e id, el orden queda indefinido y el saldo por
    fila cambia entre corridas.  El total final coincide igual, asi que hay que
    asertar los intermedios: es lo unico que distingue lo correcto de lo roto.
    """
    for amount in ("10.00", "20.00", "30.00"):
        cash.record(datetime(2026, 9, 3, 9, 0, tzinfo=timezone.utc), CashMovementType.ENTRADA, Decimal(amount))

    primera = [balance for _, balance in cash.ledger()]
    segunda = [balance for _, balance in cash.ledger()]
    assert primera == segunda
    assert primera == [Decimal("10.00"), Decimal("30.00"), Decimal("60.00")]


def test_ledger_is_empty_before_anything_is_recorded(cash):
    assert cash.ledger() == []
    assert cash.running_balance() == Decimal("0.00")
    assert cash.owner_balance() == Decimal("0.00")
