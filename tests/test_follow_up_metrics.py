"""Los tiles de "proximos N dias" no deben contar a los vencidos.

`next_7`, `next_14` y `next_30` se definian solo con cota superior (`d <= 7`),
asi que un cliente vencido hace 40 dias contaba en los tres.  Con todos los
clientes vencidos -- el estado real de la base en 2026-09-24 -- los cuatro
tiles marcaban el mismo numero y dejaban de distinguir nada.
"""

import uuid
from datetime import timedelta
from types import SimpleNamespace

from app.core.datetime_utils import business_today
from app.services.sales.orchestrator import SaleService


def _cycle(db, days_until):
    """Un ciclo de un cliente distinto, con la fecha estimada a `days_until`."""
    return SimpleNamespace(
        customer_id=uuid.uuid4(),
        product_id=uuid.uuid4(),
        due_date=business_today(db) + timedelta(days=days_until),
    )


def _service(db, cycles):
    service = SaleService(db)
    service.follow_up_repo.get_open_with_relations = lambda: cycles
    return service


def test_overdue_customers_do_not_count_as_upcoming(db_session):
    service = _service(
        db_session,
        [
            _cycle(db_session, -40),
            _cycle(db_session, 3),
            _cycle(db_session, 10),
            _cycle(db_session, 20),
            _cycle(db_session, 60),
        ],
    )

    metrics = service.get_follow_up_metrics()

    assert metrics.overdue == 1
    assert metrics.next7Days == 1  # solo el de +3
    assert metrics.next14Days == 2  # +3, +10
    assert metrics.next30Days == 3  # +3, +10, +20


def test_when_everyone_is_overdue_the_upcoming_tiles_are_empty(db_session):
    """El estado real de produccion: 12 vencidos y nadie por vencer."""
    service = _service(
        db_session,
        [
            _cycle(db_session, -68),
            _cycle(db_session, -52),
            _cycle(db_session, -41),
            _cycle(db_session, -25),
        ],
    )

    metrics = service.get_follow_up_metrics()

    assert metrics.overdue == 4
    assert (metrics.next7Days, metrics.next14Days, metrics.next30Days) == (0, 0, 0)


def test_due_today_counts_as_upcoming_not_overdue(db_session):
    service = _service(db_session, [_cycle(db_session, 0)])

    metrics = service.get_follow_up_metrics()

    assert metrics.overdue == 0
    assert (metrics.next7Days, metrics.next14Days, metrics.next30Days) == (1, 1, 1)


def test_a_customer_is_counted_by_their_most_urgent_product(db_session):
    """Dos ciclos del mismo cliente cuentan una sola vez, por el mas urgente."""
    customer_id = uuid.uuid4()
    near, far = _cycle(db_session, 2), _cycle(db_session, 25)
    near.customer_id = far.customer_id = customer_id

    metrics = _service(db_session, [near, far]).get_follow_up_metrics()

    assert (metrics.next7Days, metrics.next30Days) == (1, 1)
