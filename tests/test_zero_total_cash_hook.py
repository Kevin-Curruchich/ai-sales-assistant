"""Un documento de Q0.00 se registra; su movimiento de caja no existe.

Colision que esta rama introdujo en tres caminos a la vez: los schemas
aceptan `unitPrice = 0` y `unitCost = 0` a proposito (un regalo, una
muestra, un lote donado), `registrar_venta` lo honra explicitamente
(`is not None`, no una verdad booleana), y `CashService.record` rechaza
`amount <= 0`. Antes de esta rama `POST /sales` aceptaba una venta de cero;
con el enganche de caja pasó a reventar con un `ValueError` pelado saliendo
del service -- un 500 en un endpoint vivo.

La regla elegida: el documento vale, el movimiento no se escribe. El libro
de caja registra "dinero que realmente se movio" y cero quetzales no se
movieron; una fila de Q0.00 no cambia ningun saldo y solo ensucia el libro.
`CashService.record` conserva su guardia intacta -- es lo que atrapa un
monto negativo o vacio como error de programacion en cualquier otro
llamador; lo que cambia es que los tres caminos que derivan el monto del
total de un documento no le piden un movimiento que no existe.

Los tres caminos tienen que coincidir, por eso estan los tres aca.
"""

from datetime import date
from decimal import Decimal

import pytest

from app.models.cash_movement import CashMovement, CashMovementType
from app.models.payment_method import PaymentMethod
from app.schemas.purchase import PurchaseCreate, PurchaseItemCreate
from app.schemas.sale import (
    SaleCreate,
    SaleItemCreate,
    SalePaymentStatusUpdate,
    SaleUpdate,
)
from app.services.cash_service import CashService
from app.services.purchase_service import PurchaseService
from app.services.sales import SaleService


def _free_sale(customer, product, pending):
    """Una venta regalada: un precio unitario de cero, explicito."""
    return SaleCreate(
        customerId=customer.id,
        date=date(2026, 9, 24),
        items=[
            SaleItemCreate(
                productId=product.id,
                quantity=Decimal("1"),
                unitPrice=Decimal("0"),
            )
        ],
        isPaymentPending=pending,
    )


# --- Camino 1: SaleService.create (POST /api/v1/sales) ----------------


def test_a_paid_zero_total_sale_is_registered_without_a_cash_entry(
    db_session, seeded_customer, seeded_product_with_lot, seeded_user
):
    service = SaleService(db_session)

    sale = service.create(_free_sale(seeded_customer, seeded_product_with_lot, pending=False), user_id=seeded_user.id)

    assert sale.total == Decimal("0.00")
    assert sale.is_payment_pending is False
    assert db_session.query(CashMovement).filter_by(sale_id=sale.id).count() == 0


def test_a_paid_sale_with_a_total_above_zero_still_records_its_entry(
    db_session, seeded_customer, seeded_product_with_lot, seeded_user
):
    """La contraparte: la regla del cero no apago el enganche de caja."""
    service = SaleService(db_session)
    data = SaleCreate(
        customerId=seeded_customer.id,
        date=date(2026, 9, 24),
        items=[SaleItemCreate(productId=seeded_product_with_lot.id, quantity=Decimal("1"))],
        isPaymentPending=False,
    )

    sale = service.create(data, user_id=seeded_user.id)

    movements = db_session.query(CashMovement).filter_by(sale_id=sale.id).all()
    assert len(movements) == 1
    assert movements[0].amount == sale.total > 0


# --- Camino 2: las dos transiciones pendiente -> pagada ----------------


def test_marking_a_zero_total_sale_as_paid_records_no_cash_entry(
    db_session, seeded_customer, seeded_product_with_lot, seeded_user
):
    service = SaleService(db_session)
    sale = service.create(_free_sale(seeded_customer, seeded_product_with_lot, pending=True), user_id=seeded_user.id)

    result = service.update_payment_status_enriched(sale.id, SalePaymentStatusUpdate(isPaymentPending=False))

    assert result.is_payment_pending is False
    assert db_session.query(CashMovement).filter_by(sale_id=sale.id).count() == 0


def test_updating_a_zero_total_sale_to_paid_records_no_cash_entry(
    db_session, seeded_customer, seeded_product_with_lot, seeded_user
):
    """El mismo camino desde `PUT /api/v1/sales/{sale_id}`."""
    service = SaleService(db_session)
    sale = service.create(_free_sale(seeded_customer, seeded_product_with_lot, pending=True), user_id=seeded_user.id)

    service.update(sale.id, SaleUpdate(isPaymentPending=False))

    assert service.get_by_id(sale.id).is_payment_pending is False
    assert db_session.query(CashMovement).filter_by(sale_id=sale.id).count() == 0


# --- Camino 3: PurchaseService.confirm ---------------------------------


def test_confirming_a_zero_total_purchase_records_no_cash_exit(db_session, seeded_user, seeded_customer):
    """Un lote donado: costo unitario cero, `unitCost = 0` que el schema de
    compras acepta igual que el de ventas."""
    from app.models.product import Product

    product = Product(sku="SKU-DONADO-0001", name="Producto donado", stock=Decimal("0"))
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)

    service = PurchaseService(db_session)
    purchase = service.create(
        PurchaseCreate(
            date=date(2026, 9, 20),
            items=[PurchaseItemCreate(productId=product.id, quantity=Decimal("5"), unitCost=Decimal("0"))],
            medioPago=PaymentMethod.EFECTIVO,
        ),
        user_id=seeded_user.id,
    )

    confirmed = service.confirm(purchase.id)

    assert confirmed.total == Decimal("0.00")
    assert confirmed.status == "confirmed"
    assert db_session.query(CashMovement).filter_by(purchase_id=purchase.id).count() == 0
    # El lote entra al inventario igual: la compra ocurrio, solo no costo nada.
    db_session.refresh(product)
    assert product.stock == Decimal("5")


# --- La guardia de CashService no se toco ------------------------------


def test_cash_service_still_refuses_a_zero_amount(db_session):
    """La regla vive en los llamadores, no debilitando esta guardia: un
    `record()` de cero sigue siendo un error de programacion."""
    from datetime import datetime, timezone

    with pytest.raises(ValueError):
        CashService(db_session).record(
            occurred_at=datetime(2026, 9, 24, tzinfo=timezone.utc),
            type=CashMovementType.ENTRADA,
            amount=Decimal("0"),
        )
