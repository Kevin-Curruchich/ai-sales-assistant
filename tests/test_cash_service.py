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


@pytest.fixture
def cash_session(migrated_schema):
    session, engine = _session_for(migrated_schema)
    try:
        yield CashService(session), session
    finally:
        session.close()
        engine.dispose()


def test_running_balance_adds_entries_and_subtracts_exits(cash):
    cash.record(
        datetime(2026, 9, 3, 9, 0, tzinfo=timezone.utc),
        CashMovementType.ENTRADA,
        Decimal("115.00"),
    )
    cash.record(
        datetime(2026, 9, 3, 10, 0, tzinfo=timezone.utc),
        CashMovementType.ENTRADA,
        Decimal("37.00"),
    )
    cash.record(
        datetime(2026, 9, 5, 9, 0, tzinfo=timezone.utc),
        CashMovementType.SALIDA,
        Decimal("285.00"),
    )
    assert cash.running_balance() == Decimal("-133.00")


def test_owner_balance_is_contributions_minus_withdrawals(cash):
    cash.record(
        datetime(2026, 9, 5, 9, 0, tzinfo=timezone.utc),
        CashMovementType.APORTE_SOCIO,
        Decimal("95.00"),
    )
    cash.record(
        datetime(2026, 9, 9, 9, 0, tzinfo=timezone.utc),
        CashMovementType.APORTE_SOCIO,
        Decimal("125.02"),
    )
    cash.record(
        datetime(2026, 9, 20, 9, 0, tzinfo=timezone.utc),
        CashMovementType.RETIRO_SOCIO,
        Decimal("100.00"),
    )
    assert cash.owner_balance() == Decimal("120.02")


def test_partner_contribution_raises_the_business_balance(cash):
    """aporte_socio es dinero que entra a la caja del negocio."""
    cash.record(
        datetime(2026, 9, 5, 9, 0, tzinfo=timezone.utc),
        CashMovementType.APORTE_SOCIO,
        Decimal("95.00"),
    )
    assert cash.running_balance() == Decimal("95.00")


def test_ledger_running_balance_is_stable_with_identical_instants(cash):
    """Tres movimientos con el MISMO instante deben dar un acumulado estable.

    Con occurred_at los empates son raros, pero una carga en lote los produce.
    Sin desempate por created_at e id, el orden queda indefinido y el saldo por
    fila cambia entre corridas.  El total final coincide igual, asi que hay que
    asertar los intermedios: es lo unico que distingue lo correcto de lo roto.
    """
    for amount in ("10.00", "20.00", "30.00"):
        cash.record(
            datetime(2026, 9, 3, 9, 0, tzinfo=timezone.utc),
            CashMovementType.ENTRADA,
            Decimal(amount),
        )

    primera = [balance for _, balance in cash.ledger()]
    segunda = [balance for _, balance in cash.ledger()]
    assert primera == segunda
    assert primera == [Decimal("10.00"), Decimal("30.00"), Decimal("60.00")]


def test_ledger_is_empty_before_anything_is_recorded(cash):
    assert cash.ledger() == []
    assert cash.running_balance() == Decimal("0.00")
    assert cash.owner_balance() == Decimal("0.00")


def test_partner_withdrawal_lowers_the_business_balance(cash):
    """retiro_socio es dinero que sale de la caja del negocio, igual que un gasto.

    Sin esta prueba, un error de signo en retiro_socio solo se veria en
    owner_balance() (que ya lo cubre) pero nunca en running_balance(): de los
    cuatro tipos, este es el unico que no tenia un test dedicado a su signo
    sobre el saldo del negocio.
    """
    cash.record(
        datetime(2026, 9, 5, 9, 0, tzinfo=timezone.utc),
        CashMovementType.ENTRADA,
        Decimal("200.00"),
    )
    cash.record(
        datetime(2026, 9, 6, 9, 0, tzinfo=timezone.utc),
        CashMovementType.RETIRO_SOCIO,
        Decimal("60.00"),
    )
    assert cash.running_balance() == Decimal("140.00")


def test_ledger_last_balance_matches_running_balance_across_all_types(cash):
    """El acumulado en Python (ledger) y el agregado en SQL (running_balance) no
    deben poder divergir: son la misma cantidad calculada por dos caminos
    distintos, y solo lo siguen siendo si ambos leen el mismo conjunto de
    tipos que restan (CASH_OUTFLOW_TYPES)."""
    cash.record(
        datetime(2026, 9, 1, 9, 0, tzinfo=timezone.utc),
        CashMovementType.ENTRADA,
        Decimal("300.00"),
    )
    cash.record(
        datetime(2026, 9, 2, 9, 0, tzinfo=timezone.utc),
        CashMovementType.SALIDA,
        Decimal("50.00"),
    )
    cash.record(
        datetime(2026, 9, 3, 9, 0, tzinfo=timezone.utc),
        CashMovementType.APORTE_SOCIO,
        Decimal("95.00"),
    )
    cash.record(
        datetime(2026, 9, 4, 9, 0, tzinfo=timezone.utc),
        CashMovementType.RETIRO_SOCIO,
        Decimal("40.00"),
    )

    ledger = cash.ledger()
    assert ledger[-1][1] == cash.running_balance()
    assert ledger[-1][1] == Decimal("305.00")


def test_saldo_inicial_adds_to_the_running_balance(cash):
    # `cash` entrega un CashService pelado, no una tupla.
    cash.record(
        occurred_at=datetime(2026, 9, 24, 8, 0, tzinfo=timezone.utc),
        type=CashMovementType.SALDO_INICIAL,
        amount=Decimal("500.00"),
        note="efectivo al momento del corte",
    )
    assert cash.running_balance() == Decimal("500.00")


def test_saldo_inicial_does_not_count_as_owner_contribution(cash):
    cash.record(
        occurred_at=datetime(2026, 9, 24, 8, 0, tzinfo=timezone.utc),
        type=CashMovementType.SALDO_INICIAL,
        amount=Decimal("500.00"),
    )
    assert cash.owner_balance() == Decimal("0.00")


def test_record_without_commit_leaves_the_row_rollbackable(cash_session):
    # Fixture NUEVA de esta task: `cash` no expone la sesion y este test la
    # necesita para hacer rollback.  `cash` se deja como esta -- cambiarla a
    # tupla obligaria a tocar sus cinco call sites existentes.
    service, session = cash_session
    service.record(
        occurred_at=datetime(2026, 9, 24, 8, 0, tzinfo=timezone.utc),
        type=CashMovementType.ENTRADA,
        amount=Decimal("10.00"),
        commit=False,
    )
    session.rollback()
    assert service.running_balance() == Decimal("0.00")


def test_record_rejects_a_zero_or_negative_amount(cash):
    for bad in (Decimal("0"), Decimal("-5.00")):
        with pytest.raises(ValueError):
            cash.record(
                occurred_at=datetime(2026, 9, 24, 8, 0, tzinfo=timezone.utc),
                type=CashMovementType.ENTRADA,
                amount=bad,
            )
