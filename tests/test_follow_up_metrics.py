"""Los tiles de "proximos N dias" no deben contar a los vencidos.

`next_7`, `next_14` y `next_30` se definian solo con cota superior (`d <= 7`),
asi que un cliente vencido hace 40 dias contaba en los tres.  Con todos los
clientes vencidos -- el estado real de la base en 2026-09-24 -- los cuatro
tiles marcaban el mismo numero y dejaban de distinguir nada.
"""

import uuid
from datetime import date, timedelta
from types import SimpleNamespace

from app.services.sales.orchestrator import SaleService


def _cycle(days_until):
    """Un ciclo de un cliente distinto, con la fecha estimada a `days_until`."""
    return SimpleNamespace(
        customer_id=uuid.uuid4(),
        product_id=uuid.uuid4(),
        estimated_next_purchase=date.today() + timedelta(days=days_until),
    )


def _service(cycles):
    service = SaleService(db=None)
    service.cycle_repo.get_all_with_estimation = lambda: cycles
    return service


def test_overdue_customers_do_not_count_as_upcoming():
    service = _service([_cycle(-40), _cycle(3), _cycle(10), _cycle(20), _cycle(60)])

    metrics = service.get_follow_up_metrics()

    assert metrics.overdue == 1
    assert metrics.next7Days == 1       # solo el de +3
    assert metrics.next14Days == 2      # +3, +10
    assert metrics.next30Days == 3      # +3, +10, +20


def test_when_everyone_is_overdue_the_upcoming_tiles_are_empty():
    """El estado real de produccion: 12 vencidos y nadie por vencer."""
    service = _service([_cycle(-68), _cycle(-52), _cycle(-41), _cycle(-25)])

    metrics = service.get_follow_up_metrics()

    assert metrics.overdue == 4
    assert (metrics.next7Days, metrics.next14Days, metrics.next30Days) == (0, 0, 0)


def test_due_today_counts_as_upcoming_not_overdue():
    service = _service([_cycle(0)])

    metrics = service.get_follow_up_metrics()

    assert metrics.overdue == 0
    assert (metrics.next7Days, metrics.next14Days, metrics.next30Days) == (1, 1, 1)


def test_a_customer_is_counted_by_their_most_urgent_product():
    """Dos ciclos del mismo cliente cuentan una sola vez, por el mas urgente."""
    customer_id = uuid.uuid4()
    near, far = _cycle(2), _cycle(25)
    near.customer_id = far.customer_id = customer_id

    metrics = _service([near, far]).get_follow_up_metrics()

    assert (metrics.next7Days, metrics.next30Days) == (1, 1)
