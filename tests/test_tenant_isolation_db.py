"""Aislamiento por comercio en la base: RLS sobre un rol sin superusuario.

`db_session` corre como superusuario (se salta RLS); `tenant_session` hace
`SET ROLE app_tenant`, que es como se conecta la API en produccion.
"""

import pytest
from sqlalchemy.exc import ProgrammingError

from app.core.database import bind_business
from app.models import Customer, Product


def test_tenant_sees_only_its_business(
    db_session, tenant_session, seeded_business, other_business
):
    db_session.add(Customer(name="De A"))  # business_id por DEFAULT = A
    db_session.commit()
    bind_business(db_session, other_business.id, other_business.timezone)
    db_session.add(Customer(name="De B"))
    db_session.commit()

    bind_business(tenant_session, seeded_business.id, seeded_business.timezone)
    assert [c.name for c in tenant_session.query(Customer)] == ["De A"]


def test_tenant_without_business_sees_nothing(
    db_session, tenant_session, seeded_business
):
    db_session.add(Customer(name="De A"))
    db_session.commit()
    assert tenant_session.query(Customer).count() == 0


def test_tenant_cannot_write_into_other_business(
    tenant_session, seeded_business, other_business
):
    bind_business(tenant_session, seeded_business.id, seeded_business.timezone)
    tenant_session.add(Customer(name="x", business_id=other_business.id))
    # psycopg2: InsufficientPrivilege "new row violates row-level security policy"
    with pytest.raises(ProgrammingError, match="row-level security"):
        tenant_session.commit()


def test_same_sku_allowed_in_two_businesses(
    db_session, seeded_business, other_business
):
    db_session.add(Product(sku="SKU-1", name="De A"))
    db_session.commit()
    bind_business(db_session, other_business.id, other_business.timezone)
    db_session.add(Product(sku="SKU-1", name="De B"))
    db_session.commit()

    rows = db_session.query(Product.business_id).filter_by(sku="SKU-1").all()
    assert {r.business_id for r in rows} == {seeded_business.id, other_business.id}
