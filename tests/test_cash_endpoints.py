"""Endpoints de caja: el libro se lista del mas reciente al mas antiguo y cada
movimiento trae el saldo que dejo, aun cuando la pagina o el rango de fechas
no empiezan en el primer movimiento."""

from datetime import datetime, timezone
from decimal import Decimal

from app.models import CashMovementType
from app.services.cash_service import CashService
from tests.fixtures_http import _auth


def _at(day, hour=15):
    # 15:00 UTC = 09:00 en Guatemala: el dia calendario es el mismo en ambas zonas.
    return datetime(2026, 9, day, hour, 0, tzinfo=timezone.utc)


def _seed(db_session):
    cash = CashService(db_session)
    cash.record(
        _at(1), CashMovementType.SALDO_INICIAL, Decimal("100.00"), note="apertura"
    )
    cash.record(_at(3), CashMovementType.ENTRADA, Decimal("50.00"))
    cash.record(_at(5), CashMovementType.SALIDA, Decimal("30.00"))
    cash.record(_at(7), CashMovementType.RETIRO_SOCIO, Decimal("20.00"))


def test_lists_movements_newest_first_with_running_balance(
    client, db_session, seeded_user
):
    _seed(db_session)

    resp = client.get("/api/v1/cash/movements", headers=_auth(seeded_user))

    assert resp.status_code == 200
    body = resp.json()
    assert body["meta"]["total"] == 4
    rows = body["data"]
    assert [r["type"] for r in rows] == [
        "retiro_socio",
        "salida",
        "entrada",
        "saldo_inicial",
    ]
    assert [Decimal(r["running_balance"]) for r in rows] == [
        Decimal("100.00"),
        Decimal("120.00"),
        Decimal("150.00"),
        Decimal("100.00"),
    ]
    assert [r["is_outflow"] for r in rows] == [True, True, False, False]
    assert rows[-1]["note"] == "apertura"


def test_pagination_keeps_the_balance_of_the_whole_ledger(
    client, db_session, seeded_user
):
    _seed(db_session)

    resp = client.get(
        "/api/v1/cash/movements?limit=2&offset=2", headers=_auth(seeded_user)
    )

    rows = resp.json()["data"]
    assert [r["type"] for r in rows] == ["entrada", "saldo_inicial"]
    assert [Decimal(r["running_balance"]) for r in rows] == [
        Decimal("150.00"),
        Decimal("100.00"),
    ]


def test_date_range_filters_rows_but_not_the_balance(client, db_session, seeded_user):
    """El saldo de un movimiento no depende del rango pedido: arrastra lo anterior."""
    _seed(db_session)

    resp = client.get(
        "/api/v1/cash/movements?start_date=2026-09-03&end_date=2026-09-05",
        headers=_auth(seeded_user),
    )

    body = resp.json()
    assert body["meta"]["total"] == 2
    assert [r["type"] for r in body["data"]] == ["salida", "entrada"]
    assert [Decimal(r["running_balance"]) for r in body["data"]] == [
        Decimal("120.00"),
        Decimal("150.00"),
    ]


def test_end_date_includes_the_whole_business_day(client, db_session, seeded_user):
    """23:00 en Guatemala ya es el dia siguiente en UTC; sigue contando para ese dia."""
    CashService(db_session).record(
        datetime(2026, 9, 10, 5, 0, tzinfo=timezone.utc),
        CashMovementType.ENTRADA,
        Decimal("10.00"),
    )

    resp = client.get(
        "/api/v1/cash/movements?end_date=2026-09-09", headers=_auth(seeded_user)
    )

    assert resp.json()["meta"]["total"] == 1


def test_summary_returns_operating_and_owner_balances(client, db_session, seeded_user):
    _seed(db_session)
    CashService(db_session).record(
        _at(8), CashMovementType.APORTE_SOCIO, Decimal("70.00")
    )

    resp = client.get("/api/v1/cash/summary", headers=_auth(seeded_user))

    assert resp.status_code == 200
    assert Decimal(resp.json()["balance"]) == Decimal("170.00")
    assert Decimal(resp.json()["owner_balance"]) == Decimal("50.00")


def test_requires_authentication(client):
    assert client.get("/api/v1/cash/movements").status_code in (401, 403)
