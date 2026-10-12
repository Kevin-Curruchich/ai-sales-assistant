"""Cobrar una venta a credito desde el agente: `consultar_ventas` encuentra la
venta y `registrar_cobro` la marca pagada.

Hasta esta tool el agente podia registrar una venta con `pago_pendiente=True`
pero no anotar que la pagaron: la caja quedaba corta y la venta seguia
contando como cuenta por cobrar hasta que alguien iba al panel. El cobro
reusa `SaleService.mark_as_paid`, la misma transicion que el PATCH del panel,
asi que la ENTRADA de caja y la venta pagada son una sola transaccion.
"""

from datetime import date, datetime
from decimal import Decimal

import pytest

from app.agent.auth import AgentAuthError
from app.agent.tools.read import consultar_ventas
from app.agent.tools.write import registrar_cobro
from app.core.datetime_utils import business_midnight, business_tz
from app.models.cash_movement import CashMovement, CashMovementType
from app.schemas.sale import SaleCreate, SaleItemCreate
from app.services.sales import SaleService


def _config(user):
    return {"configurable": {"user_id": str(user.id)}}


def _create_sale(
    db_session, user, customer, product, *, pending, day=date(2026, 9, 24)
):
    return SaleService(db_session).create(
        SaleCreate(
            customerId=customer.id,
            date=day,
            items=[SaleItemCreate(productId=product.id, quantity=Decimal("1"))],
            isPaymentPending=pending,
        ),
        user_id=user.id,
    )


@pytest.fixture
def pending_sale(db_session, seeded_user, seeded_customer, seeded_product_with_lot):
    return _create_sale(
        db_session, seeded_user, seeded_customer, seeded_product_with_lot, pending=True
    )


@pytest.fixture
def paid_sale(db_session, seeded_user, seeded_customer, seeded_product_with_lot):
    return _create_sale(
        db_session, seeded_user, seeded_customer, seeded_product_with_lot, pending=False
    )


# ---------------------------------------------------------------------
# consultar_ventas
# ---------------------------------------------------------------------


def test_consultar_ventas_lists_only_pending_sales_when_asked(
    db_session, pending_sale, paid_sale
):
    result = consultar_ventas.invoke({"estado_pago": "pendiente"})

    assert [v["id"] for v in result["ventas"]] == [str(pending_sale.id)]
    assert result["total"] == 1
    assert result["monto_total"] == str(pending_sale.total)
    venta = result["ventas"][0]
    assert venta["pendiente"] is True
    assert venta["cliente"] == "Cliente de prueba"
    assert venta["items"][0]["producto"].startswith("Producto de prueba")


def test_consultar_ventas_sums_every_match_not_only_the_page(
    db_session, seeded_user, seeded_customer, seeded_product_with_lot
):
    """`monto_total` es lo que se debe en total, no lo que entro en la pagina:
    con `limite=1` el agente diria "te deben Q40" cuando son Q80."""
    a = _create_sale(
        db_session, seeded_user, seeded_customer, seeded_product_with_lot, pending=True
    )
    b = _create_sale(
        db_session, seeded_user, seeded_customer, seeded_product_with_lot, pending=True
    )

    result = consultar_ventas.invoke({"estado_pago": "pendiente", "limite": 1})

    assert len(result["ventas"]) == 1
    assert result["total"] == 2
    assert result["hay_mas"] is True
    assert Decimal(result["monto_total"]) == a.total + b.total


def test_consultar_ventas_reports_how_many_days_a_sale_has_been_owed(
    db_session, pending_sale
):
    hoy = datetime.now(business_tz()).date()

    venta = consultar_ventas.invoke({"estado_pago": "pendiente"})["ventas"][0]

    assert venta["dias_pendiente"] == (hoy - pending_sale.date).days


def test_consultar_ventas_has_no_days_owed_for_a_paid_sale(db_session, paid_sale):
    venta = consultar_ventas.invoke({"estado_pago": "pagada"})["ventas"][0]

    assert venta["pendiente"] is False
    assert venta["dias_pendiente"] is None


def test_consultar_ventas_filters_by_customer(
    db_session,
    seeded_user,
    seeded_customer,
    two_similar_customers,
    seeded_product_with_lot,
):
    otro = two_similar_customers[0]
    _create_sale(db_session, seeded_user, otro, seeded_product_with_lot, pending=True)
    mia = _create_sale(
        db_session, seeded_user, seeded_customer, seeded_product_with_lot, pending=True
    )

    result = consultar_ventas.invoke({"cliente_id": str(seeded_customer.id)})

    assert [v["id"] for v in result["ventas"]] == [str(mia.id)]


def test_consultar_ventas_filters_by_date_range(
    db_session, seeded_user, seeded_customer, seeded_product_with_lot
):
    _create_sale(
        db_session,
        seeded_user,
        seeded_customer,
        seeded_product_with_lot,
        pending=True,
        day=date(2026, 9, 10),
    )
    dentro = _create_sale(
        db_session,
        seeded_user,
        seeded_customer,
        seeded_product_with_lot,
        pending=True,
        day=date(2026, 9, 24),
    )

    result = consultar_ventas.invoke({"desde": "2026-09-20", "hasta": "2026-09-24"})

    assert [v["id"] for v in result["ventas"]] == [str(dentro.id)]


def test_consultar_ventas_rejects_an_unknown_payment_status(db_session):
    """Un filtro mal escrito no puede caer en "todas": el agente contestaria
    "quien me debe" con ventas ya cobradas."""
    with pytest.raises(ValueError):
        consultar_ventas.invoke({"estado_pago": "debe"})


def test_consultar_ventas_writes_nothing(db_session, pending_sale):
    consultar_ventas.invoke({})
    assert db_session.query(CashMovement).count() == 0
    db_session.refresh(pending_sale)
    assert pending_sale.is_payment_pending is True


# ---------------------------------------------------------------------
# registrar_cobro
# ---------------------------------------------------------------------


def test_an_approved_cobro_marks_the_sale_paid_and_records_the_cash_entry(
    db_session, pending_sale, seeded_user, monkeypatch
):
    monkeypatch.setattr(
        "app.agent.tools.write.interrupt", lambda _p: {"accion": "aprobar"}
    )

    result = registrar_cobro.invoke(
        {
            "venta_id": str(pending_sale.id),
            "fecha_pago": "2026-09-30",
            "medio_pago": "transferencia",
        },
        config=_config(seeded_user),
    )

    assert result["estado"] == "registrado"
    db_session.refresh(pending_sale)
    assert pending_sale.is_payment_pending is False
    assert pending_sale.payment_date == date(2026, 9, 30)
    assert pending_sale.payment_method.value == "transferencia"

    movement = db_session.query(CashMovement).filter_by(sale_id=pending_sale.id).one()
    assert movement.type == CashMovementType.ENTRADA
    assert movement.amount == pending_sale.total
    # El cobro entra a caja el dia que la persona dice, en hora de Guatemala
    # -- no el dia del servidor, que es lo que hace hoy el PATCH del panel.
    assert movement.occurred_at == business_midnight(date(2026, 9, 30))


def test_the_confirmation_card_shows_what_is_being_collected(
    db_session, pending_sale, seeded_user, monkeypatch
):
    seen = {}

    def capture(payload):
        seen.update(payload)
        return {"accion": "cancelar"}

    monkeypatch.setattr("app.agent.tools.write.interrupt", capture)

    registrar_cobro.invoke(
        {"venta_id": str(pending_sale.id), "fecha_pago": "2026-09-30"},
        config=_config(seeded_user),
    )

    assert seen["tipo"] == "confirmar_cobro"
    assert "huella" not in seen
    cobro = seen["cobro"]
    assert cobro["venta_id"] == str(pending_sale.id)
    assert cobro["cliente"] == "Cliente de prueba"
    assert cobro["fecha_venta"] == "2026-09-24"
    assert cobro["total"] == str(pending_sale.total)
    assert cobro["fecha_pago"] == "2026-09-30"
    assert cobro["medio_pago"] == "efectivo"


def test_a_cancelled_cobro_writes_nothing(
    db_session, pending_sale, seeded_user, monkeypatch
):
    monkeypatch.setattr(
        "app.agent.tools.write.interrupt", lambda _p: {"accion": "cancelar"}
    )

    result = registrar_cobro.invoke(
        {"venta_id": str(pending_sale.id), "fecha_pago": "2026-09-30"},
        config=_config(seeded_user),
    )

    assert result["estado"] == "cancelado"
    db_session.refresh(pending_sale)
    assert pending_sale.is_payment_pending is True
    assert db_session.query(CashMovement).count() == 0


def test_an_unrecognized_decision_on_a_cobro_writes_nothing(
    db_session, pending_sale, seeded_user, monkeypatch
):
    monkeypatch.setattr(
        "app.agent.tools.write.interrupt", lambda _p: {"accion": "quizas"}
    )

    result = registrar_cobro.invoke(
        {"venta_id": str(pending_sale.id), "fecha_pago": "2026-09-30"},
        config=_config(seeded_user),
    )

    assert result["estado"] == "cancelado"
    assert db_session.query(CashMovement).count() == 0


def test_correcting_a_cobro_asks_again_with_the_new_values(
    db_session, pending_sale, seeded_user, monkeypatch
):
    payloads = []
    decisions = iter(
        [
            {
                "accion": "corregir",
                "valores": {"fecha_pago": "2026-09-28", "medio_pago": "transferencia"},
            },
            {"accion": "aprobar"},
        ]
    )

    def record_and_answer(payload):
        payloads.append(payload)
        return next(decisions)

    monkeypatch.setattr("app.agent.tools.write.interrupt", record_and_answer)

    result = registrar_cobro.invoke(
        {"venta_id": str(pending_sale.id), "fecha_pago": "2026-09-30"},
        config=_config(seeded_user),
    )

    assert result["estado"] == "registrado"
    assert len(payloads) == 2
    assert payloads[1]["cobro"]["fecha_pago"] == "2026-09-28"
    db_session.refresh(pending_sale)
    assert pending_sale.payment_date == date(2026, 9, 28)
    assert pending_sale.payment_method.value == "transferencia"


def test_an_already_paid_sale_is_reported_without_asking_for_approval(
    db_session, paid_sale, seeded_user, monkeypatch
):
    """Pedirle a alguien que apruebe un cobro que no se va a poder escribir es
    el orden equivocado -- mismo criterio que la autenticacion."""
    calls = {"n": 0}

    def spy(_p):
        calls["n"] += 1
        return {"accion": "aprobar"}

    monkeypatch.setattr("app.agent.tools.write.interrupt", spy)

    result = registrar_cobro.invoke(
        {"venta_id": str(paid_sale.id), "fecha_pago": "2026-09-30"},
        config=_config(seeded_user),
    )

    assert result["estado"] == "ya_pagada"
    assert calls["n"] == 0
    assert db_session.query(CashMovement).filter_by(sale_id=paid_sale.id).count() == 1


def test_a_sale_paid_from_the_panel_while_waiting_for_approval_is_not_collected_twice(
    db_session, pending_sale, seeded_user, monkeypatch
):
    """Entre la tarjeta y la aprobacion, alguien la marco pagada en el panel.
    Aprobar despues no puede registrar una segunda ENTRADA."""

    def paid_elsewhere_then_approve(_p):
        SaleService(db_session).mark_as_paid(
            SaleService(db_session).get_by_id(pending_sale.id), date(2026, 9, 29), None
        )
        db_session.commit()
        return {"accion": "aprobar"}

    monkeypatch.setattr("app.agent.tools.write.interrupt", paid_elsewhere_then_approve)

    result = registrar_cobro.invoke(
        {"venta_id": str(pending_sale.id), "fecha_pago": "2026-09-30"},
        config=_config(seeded_user),
    )

    assert result["estado"] == "ya_pagada"
    assert (
        db_session.query(CashMovement).filter_by(sale_id=pending_sale.id).count() == 1
    )


def test_an_unknown_sale_is_reported_without_asking_for_approval(
    db_session, seeded_user, monkeypatch
):
    calls = {"n": 0}

    def spy(_p):
        calls["n"] += 1
        return {"accion": "aprobar"}

    monkeypatch.setattr("app.agent.tools.write.interrupt", spy)

    result = registrar_cobro.invoke(
        {
            "venta_id": "00000000-0000-0000-0000-000000000000",
            "fecha_pago": "2026-09-30",
        },
        config=_config(seeded_user),
    )

    assert result["estado"] == "no_encontrada"
    assert calls["n"] == 0


def test_registrar_cobro_authenticates_before_asking_for_approval(
    db_session, pending_sale, monkeypatch
):
    calls = {"n": 0}

    def spy(_p):
        calls["n"] += 1
        return {"accion": "aprobar"}

    monkeypatch.setattr("app.agent.tools.write.interrupt", spy)

    with pytest.raises(AgentAuthError):
        registrar_cobro.invoke(
            {"venta_id": str(pending_sale.id), "fecha_pago": "2026-09-30"}
        )

    assert calls["n"] == 0
    db_session.refresh(pending_sale)
    assert pending_sale.is_payment_pending is True


def test_an_invalid_payment_method_fails_before_asking_for_approval(
    db_session, pending_sale, seeded_user, monkeypatch
):
    calls = {"n": 0}

    def spy(_p):
        calls["n"] += 1
        return {"accion": "aprobar"}

    monkeypatch.setattr("app.agent.tools.write.interrupt", spy)

    with pytest.raises(ValueError):
        registrar_cobro.invoke(
            {
                "venta_id": str(pending_sale.id),
                "fecha_pago": "2026-09-30",
                "medio_pago": "cheque",
            },
            config=_config(seeded_user),
        )

    assert calls["n"] == 0


def test_a_cobro_replayed_by_the_same_task_is_not_written_twice(
    db_session, pending_sale, seeded_user, monkeypatch
):
    """Misma tarea de Pregel, segunda pasada (proceso que murio despues del
    commit): devuelve `ya_registrado` sin volver a pausar."""
    config = {
        "configurable": {
            "user_id": str(seeded_user.id),
            "thread_id": "hilo-cobro",
            "checkpoint_ns": f"tools:{pending_sale.id}",
        }
    }
    monkeypatch.setattr(
        "app.agent.tools.write.interrupt", lambda _p: {"accion": "aprobar"}
    )
    first = registrar_cobro.invoke(
        {"venta_id": str(pending_sale.id), "fecha_pago": "2026-09-30"}, config=config
    )
    assert first["estado"] == "registrado"

    calls = {"n": 0}

    def spy(_p):
        calls["n"] += 1
        return {"accion": "aprobar"}

    monkeypatch.setattr("app.agent.tools.write.interrupt", spy)
    second = registrar_cobro.invoke(
        {"venta_id": str(pending_sale.id), "fecha_pago": "2026-09-30"}, config=config
    )

    assert second["estado"] == "ya_registrado"
    assert second["venta_id"] == str(pending_sale.id)
    assert calls["n"] == 0
    assert (
        db_session.query(CashMovement).filter_by(sale_id=pending_sale.id).count() == 1
    )


def test_a_zero_total_sale_is_marked_paid_without_a_cash_entry(
    db_session, seeded_user, seeded_customer, seeded_product_with_lot, monkeypatch
):
    """Un regalo a credito: el documento se cobra, el movimiento no existe
    (misma regla del total cero que `_record_sale_cash_entry`)."""
    sale = SaleService(db_session).create(
        SaleCreate(
            customerId=seeded_customer.id,
            date=date(2026, 9, 24),
            items=[
                SaleItemCreate(
                    productId=seeded_product_with_lot.id,
                    quantity=Decimal("1"),
                    unitPrice=Decimal("0"),
                )
            ],
            isPaymentPending=True,
        ),
        user_id=seeded_user.id,
    )
    monkeypatch.setattr(
        "app.agent.tools.write.interrupt", lambda _p: {"accion": "aprobar"}
    )

    result = registrar_cobro.invoke(
        {"venta_id": str(sale.id), "fecha_pago": "2026-09-30"},
        config=_config(seeded_user),
    )

    assert result["estado"] == "registrado"
    db_session.refresh(sale)
    assert sale.is_payment_pending is False
    assert db_session.query(CashMovement).filter_by(sale_id=sale.id).count() == 0
