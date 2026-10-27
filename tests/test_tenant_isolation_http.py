"""Aislamiento por comercio de punta a punta: la API, sobre una sesion sujeta a
RLS (`tenant_client`), no le muestra a un comercio nada del otro."""

import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.core.database import bind_business
from app.models import Customer, Product, Sale
from app.models.agent_thread import AgentThread
from app.models.cash_movement import CashMovement, CashMovementType
from app.models.follow_up import FollowUp
from app.models.purchase import Purchase
from app.repositories.agent_thread_repository import AgentThreadRepository
from tests.fixtures_http import _auth


def _seed(db_session, user, business, marker: str) -> dict:
    """Un registro por recurso en `business`, todos con `marker` en un texto
    visible o en su id. Devuelve los ids por ruta."""
    bind_business(db_session, business.id, business.timezone)
    customer = Customer(name=f"Cliente {marker}")
    product = Product(
        sku=f"SKU-{marker}", name=f"Producto {marker}", stock=Decimal("1")
    )
    db_session.add_all([customer, product])
    db_session.flush()
    sale = Sale(
        customer_id=customer.id, user_id=user.id, date=date(2026, 10, 1), total=10
    )
    purchase = Purchase(
        user_id=user.id, date=date(2026, 10, 1), supplier_name=f"Proveedor {marker}"
    )
    movement = CashMovement(
        occurred_at=datetime(2026, 10, 1, 12, tzinfo=timezone.utc),
        type=CashMovementType.APORTE_SOCIO,
        amount=Decimal("5.00"),
        note=f"Nota {marker}",
    )
    follow_up = FollowUp(
        customer_id=customer.id, product_id=product.id, due_date=date(2026, 10, 2)
    )
    thread = AgentThread(owner_id=user.id, title=f"Hilo {marker}")
    db_session.add_all([sale, purchase, movement, follow_up, thread])
    db_session.commit()
    return {
        "/api/v1/customers": customer.id,
        "/api/v1/products": product.id,
        "/api/v1/sales": sale.id,
        "/api/v1/purchases": purchase.id,
        "/api/v1/cash/movements": movement.id,
        "/api/v1/follow-ups": follow_up.id,
        "/api/v1/agent/threads": thread.id,
    }


@pytest.fixture
def data_in_both_businesses(
    db_session, seeded_user, seeded_business, user_of_other_business, other_business
):
    marker_b = f"B-{uuid.uuid4().hex[:8]}"
    ids_a = _seed(db_session, seeded_user, seeded_business, "A")
    ids_b = _seed(db_session, user_of_other_business, other_business, marker_b)
    bind_business(db_session, seeded_business.id, seeded_business.timezone)
    return SimpleNamespace(marker_b=marker_b, ids_a=ids_a, ids_b=ids_b)


LIST_PATHS = [
    "/api/v1/customers",
    "/api/v1/products",
    "/api/v1/sales",
    "/api/v1/purchases",
    "/api/v1/cash/movements",
    "/api/v1/follow-ups",
    "/api/v1/agent/threads",
]


@pytest.mark.parametrize("path", LIST_PATHS)
def test_list_endpoints_hide_other_business(
    tenant_client, path, data_in_both_businesses, seeded_user
):
    resp = tenant_client.get(path, headers=_auth(seeded_user))
    assert resp.status_code == 200, resp.text
    body = resp.text
    # Sin esto, una lista vacia (o un filtro por defecto) pasaria el test.
    assert str(data_in_both_businesses.ids_a[path]) in body
    assert str(data_in_both_businesses.ids_b[path]) not in body
    assert data_in_both_businesses.marker_b not in body


@pytest.mark.parametrize(
    "path",
    ["/api/v1/customers", "/api/v1/products", "/api/v1/sales", "/api/v1/purchases"],
)
def test_get_by_id_of_other_business_is_404(
    tenant_client, tenant_session, path, data_in_both_businesses, seeded_user
):
    tenant_session.expunge_all()
    resp = tenant_client.get(
        f"{path}/{data_in_both_businesses.ids_b[path]}", headers=_auth(seeded_user)
    )
    assert resp.status_code == 404, resp.text


def test_rls_hides_thread_of_other_business(
    tenant_session, seeded_business, data_in_both_businesses
):
    """El 404 del hilo ajeno lo da tambien el chequeo de dueño; esto prueba que
    la base, por si sola, ya no lo devuelve."""
    bind_business(tenant_session, seeded_business.id, seeded_business.timezone)
    repo = AgentThreadRepository(tenant_session)
    ids = data_in_both_businesses
    assert repo.get(ids.ids_a["/api/v1/agent/threads"]) is not None
    assert repo.get(ids.ids_b["/api/v1/agent/threads"]) is None


MODELS = {
    "/api/v1/customers": Customer,
    "/api/v1/products": Product,
    "/api/v1/sales": Sale,
    "/api/v1/purchases": Purchase,
    "/api/v1/follow-ups": FollowUp,
    "/api/v1/agent/threads": AgentThread,
}

# Toda mutacion por id que existe en app/api/v1/endpoints/ sobre datos con RLS.
# (Las de /auth/users estan en test_auth_tenancy.py: `users` no tiene RLS.
# Caja no tiene mutaciones por id.)
MUTATIONS = [
    ("PUT", "/api/v1/customers", "", {"name": "pirata"}),
    ("PUT", "/api/v1/products", "", {"name": "pirata"}),
    ("DELETE", "/api/v1/products", "", None),
    ("PUT", "/api/v1/sales", "", {"isPaymentPending": True}),
    ("PATCH", "/api/v1/sales", "/payment-status", {"isPaymentPending": True}),
    ("PUT", "/api/v1/purchases", "", {"supplierName": "pirata"}),
    ("POST", "/api/v1/purchases", "/confirm", None),
    ("POST", "/api/v1/purchases", "/cancel", None),
    ("DELETE", "/api/v1/purchases", "", None),
    ("POST", "/api/v1/follow-ups", "/postpone", {"date": "2099-01-01"}),
    ("POST", "/api/v1/follow-ups", "/dismiss", None),
    ("PATCH", "/api/v1/agent/threads", "", {"title": "pirata"}),
    ("DELETE", "/api/v1/agent/threads", "", None),
]


def _snapshot(db_session, model, row_id) -> dict | None:
    db_session.expire_all()
    row = db_session.get(model, row_id)
    if row is None:
        return None
    return {c.key: getattr(row, c.key) for c in model.__mapper__.column_attrs}


@pytest.mark.parametrize(
    "method,path,suffix,body",
    MUTATIONS,
    ids=[f"{m} {p}/{{id}}{s}" for m, p, s, _ in MUTATIONS],
)
def test_mutation_of_other_business_is_404_and_leaves_row_intact(
    tenant_client,
    tenant_session,
    db_session,
    method,
    path,
    suffix,
    body,
    data_in_both_businesses,
    seeded_user,
):
    tenant_session.expunge_all()
    row_id = data_in_both_businesses.ids_b[path]
    before = _snapshot(db_session, MODELS[path], row_id)
    assert before is not None

    resp = tenant_client.request(
        method, f"{path}/{row_id}{suffix}", json=body, headers=_auth(seeded_user)
    )

    assert resp.status_code == 404, resp.text
    assert _snapshot(db_session, MODELS[path], row_id) == before
