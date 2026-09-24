import uuid
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker

from app.models import Customer, CustomerProductCycle, Product, Sale, SaleItem, User
from app.services.sales.projection import project
from scripts.backfill_projections import _guard_against_protected_schema, backfill_projections
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


def _seed_pair(session, sale_dates: list[date], **cycle_overrides):
    """Un cliente, un producto y una venta por fecha.

    `cycle_overrides` permite sembrar un ciclo ya consistente con lo que
    `project()` calcularia (para probar el bucket `unchanged`), en vez del
    valor inventado que usan por defecto el resto de los tests.
    """
    user = User(email=f"{uuid.uuid4().hex}@example.com", role="admin")
    customer = Customer(name="Cecy")
    product = Product(sku=f"SKU-{uuid.uuid4().hex[:6]}", name="Cilindro de gas")
    session.add_all([user, customer, product])
    session.flush()

    for d in sale_dates:
        sale = Sale(customer_id=customer.id, user_id=user.id, date=d, total=Decimal("115.00"))
        session.add(sale)
        session.flush()
        session.add(
            SaleItem(
                sale_id=sale.id, product_id=product.id, quantity=Decimal("1"),
                unit_price=Decimal("115.00"), subtotal=Decimal("115.00"),
            )
        )

    fields = dict(
        customer_id=customer.id, product_id=product.id,
        avg_interval_days=Decimal("30"),          # el valor inventado que hay que corregir
        estimated_next_purchase=date(2099, 1, 1),
        last_purchase_date=sale_dates[-1], last_quantity=Decimal("1"),
        total_purchases=len(sale_dates),
    )
    fields.update(cycle_overrides)
    cycle = CustomerProductCycle(**fields)
    session.add(cycle)
    session.commit()
    return cycle


@pytest.fixture
def session(migrated_schema):
    s, engine = _session_for(migrated_schema)
    try:
        yield s
    finally:
        s.close()
        engine.dispose()


def test_recomputes_the_interval_from_history(session):
    cycle = _seed_pair(session, [date(2026, 8, 1), date(2026, 8, 11), date(2026, 8, 21)])
    backfill_projections(session)
    session.refresh(cycle)
    assert cycle.avg_interval_days == Decimal("10.0000")
    assert cycle.estimated_next_purchase == date(2026, 8, 31)
    assert cycle.projection_method == "ewma"
    assert cycle.projection_confidence == "low"


def test_clears_the_invented_date_for_a_single_purchase(session):
    cycle = _seed_pair(session, [date(2026, 8, 1)])
    result = backfill_projections(session)
    session.refresh(cycle)
    assert cycle.estimated_next_purchase is None
    assert cycle.avg_interval_days is None
    assert cycle.projection_confidence == "insufficient"
    assert result["cleared"] == 1


def test_is_idempotent(session):
    cycle = _seed_pair(session, [date(2026, 8, 1), date(2026, 8, 11), date(2026, 8, 21)])
    backfill_projections(session)
    session.refresh(cycle)
    first = (cycle.avg_interval_days, cycle.estimated_next_purchase)

    backfill_projections(session)
    session.refresh(cycle)
    assert (cycle.avg_interval_days, cycle.estimated_next_purchase) == first


def test_dry_run_writes_nothing(session):
    cycle = _seed_pair(session, [date(2026, 8, 1), date(2026, 8, 11), date(2026, 8, 21)])
    before = cycle.avg_interval_days

    result = backfill_projections(session, dry_run=True)
    session.refresh(cycle)

    assert cycle.avg_interval_days == before
    assert result["updated"] == 1


def test_all_three_counters_in_a_single_run(session):
    """updated + cleared + unchanged, sembrados juntos, en una sola corrida.

    Antes del fix, `unchanged` solo comparaba cuatro de los cinco campos que
    escribe el bloque de write, asi que un total_purchases desactualizado
    podia colarse como "sin cambios". Este test siembra un tercer ciclo ya
    consistente con lo que `project()` calcularia -- incluido total_purchases
    -- para que el bucket `unchanged` este genuinamente verificado, no solo
    inferido por ausencia de las otras dos categorias.
    """
    three_dates = [date(2026, 8, 1), date(2026, 8, 11), date(2026, 8, 21)]

    needs_update = _seed_pair(session, three_dates)
    needs_clearing = _seed_pair(session, [date(2026, 8, 5)])

    interval, next_date, method, confidence = project(three_dates)
    already_consistent = _seed_pair(
        session,
        three_dates,
        avg_interval_days=interval,
        estimated_next_purchase=next_date,
        projection_method=method,
        projection_confidence=confidence,
    )

    result = backfill_projections(session)

    assert result == {"updated": 1, "cleared": 1, "unchanged": 1}

    session.refresh(needs_update)
    assert needs_update.avg_interval_days == interval
    session.refresh(needs_clearing)
    assert needs_clearing.estimated_next_purchase is None
    session.refresh(already_consistent)
    assert already_consistent.avg_interval_days == interval
    assert already_consistent.estimated_next_purchase == next_date


def test_guard_allows_a_dry_run_against_the_protected_schema(monkeypatch):
    monkeypatch.delenv("REVENEW_ALLOW_DB_DEV", raising=False)
    _guard_against_protected_schema("db_dev", dry_run=True)  # no debe levantar


def test_guard_blocks_a_real_run_against_the_protected_schema(monkeypatch):
    monkeypatch.delenv("REVENEW_ALLOW_DB_DEV", raising=False)
    with pytest.raises(RuntimeError, match="db_dev"):
        _guard_against_protected_schema("db_dev", dry_run=False)


def test_guard_allows_a_real_run_with_the_explicit_opt_in(monkeypatch):
    monkeypatch.setenv("REVENEW_ALLOW_DB_DEV", "1")
    _guard_against_protected_schema("db_dev", dry_run=False)  # no debe levantar


def test_guard_allows_a_real_run_against_any_other_schema(monkeypatch):
    monkeypatch.delenv("REVENEW_ALLOW_DB_DEV", raising=False)
    _guard_against_protected_schema("db_v2", dry_run=False)  # no debe levantar
