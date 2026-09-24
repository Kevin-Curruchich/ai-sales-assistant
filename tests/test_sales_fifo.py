from dataclasses import dataclass
from decimal import Decimal

import pytest

from app.services.sales.fifo import InsufficientLots, allocate_fifo, check_availability


@dataclass
class FakeLot:
    """Basta con remaining_quantity y unit_cost: allocate_fifo no toca nada mas."""

    remaining_quantity: Decimal
    unit_cost: Decimal


def test_consumes_the_oldest_lot_first():
    lots = [FakeLot(Decimal("6"), Decimal("33.50")), FakeLot(Decimal("6"), Decimal("35.00"))]
    allocations, cost_basis = allocate_fifo(lots, Decimal("4"))
    assert [(id(l), q) for l, q in allocations] == [(id(lots[0]), Decimal("4"))]
    assert cost_basis == Decimal("33.50")


def test_spanning_two_lots_gives_a_weighted_cost():
    """2 a Q33.50 y 2 a Q35.00 -> (67.00 + 70.00) / 4 = 34.25"""
    lots = [FakeLot(Decimal("2"), Decimal("33.50")), FakeLot(Decimal("6"), Decimal("35.00"))]
    allocations, cost_basis = allocate_fifo(lots, Decimal("4"))
    assert [q for _, q in allocations] == [Decimal("2"), Decimal("2")]
    assert cost_basis == Decimal("34.25")


def test_half_carton_takes_from_a_single_lot():
    lots = [FakeLot(Decimal("6"), Decimal("34.17"))]
    allocations, cost_basis = allocate_fifo(lots, Decimal("0.5"))
    assert [q for _, q in allocations] == [Decimal("0.5")]
    assert cost_basis == Decimal("34.17")


def test_requesting_more_than_every_lot_combined_raises():
    """Contrato que hoy vive como HTTPException 409 y debe sobrevivir."""
    lots = [FakeLot(Decimal("2"), Decimal("95.00"))]
    with pytest.raises(InsufficientLots) as excinfo:
        allocate_fifo(lots, Decimal("100"))
    assert excinfo.value.requested == Decimal("100")
    assert excinfo.value.available == Decimal("2")


def test_lots_with_nothing_left_are_skipped():
    lots = [FakeLot(Decimal("0"), Decimal("33.50")), FakeLot(Decimal("3"), Decimal("35.00"))]
    allocations, cost_basis = allocate_fifo(lots, Decimal("1"))
    assert [q for _, q in allocations] == [Decimal("1")]
    assert cost_basis == Decimal("35.00")


@pytest.mark.parametrize("bad_quantity", [Decimal("0"), Decimal("-2")])
def test_allocate_fifo_rejects_non_positive_quantity(bad_quantity):
    """Sin esta guarda, 0 explota con InvalidOperation y un negativo devuelve
    ([], -0.00) sin excepcion: una asignacion vacia con costo cero que hace ver
    la venta como margen puro. Este modulo lo llama un agente sin Pydantic
    por delante, asi que la guarda tiene que vivir aqui."""
    lots = [FakeLot(Decimal("6"), Decimal("33.50"))]
    with pytest.raises(ValueError):
        allocate_fifo(lots, bad_quantity)


def test_stock_without_lots_is_reported_not_invented():
    """La skill verificar-lote-disponible lo exige explicitamente."""
    ok, problem = check_availability(lots=[], stock_actual=Decimal("5"), requested=Decimal("1"))
    assert ok is False
    assert "sin lotes" in problem.lower()


def test_availability_passes_when_lots_cover_the_request():
    lots = [FakeLot(Decimal("3"), Decimal("95.00"))]
    ok, problem = check_availability(lots=lots, stock_actual=Decimal("3"), requested=Decimal("2"))
    assert ok is True
    assert problem is None
