from decimal import ROUND_HALF_UP, Decimal
from typing import Optional, Protocol

MONEY = Decimal("0.01")


class Lot(Protocol):
    """Lo unico que allocate_fifo necesita de un lote."""

    remaining_quantity: Decimal
    unit_cost: Decimal


class InsufficientLots(Exception):
    """Los lotes disponibles no alcanzan para la cantidad pedida.

    Es un error de dominio, no HTTP: el orquestador lo traduce a 409 para la
    API, y el agente lo recibe como excepcion normal.
    """

    def __init__(self, requested: Decimal, available: Decimal):
        self.requested = requested
        self.available = available
        super().__init__(
            f"Lotes FIFO insuficientes: disponible={available}, pedido={requested}"
        )


def _money(value: Decimal) -> Decimal:
    return Decimal(str(value)).quantize(MONEY, rounding=ROUND_HALF_UP)


def allocate_fifo(
    lots: list[Lot], quantity: Decimal
) -> tuple[list[tuple[Lot, Decimal]], Decimal]:
    """Consume del lote mas antiguo con existencia.

    `lots` llega ya ordenado por fecha de compra ascendente — ordenarlo es
    responsabilidad del repositorio, no de este calculo.  Devuelve las
    asignaciones y el costo unitario ponderado por las cantidades tomadas.
    """
    if quantity <= 0:
        raise ValueError(
            f"La cantidad a asignar debe ser mayor que cero, recibido={quantity}"
        )

    to_consume = Decimal(str(quantity))
    allocations: list[tuple[Lot, Decimal]] = []
    total_cost = Decimal("0.00")

    for lot in lots:
        if to_consume <= 0:
            break
        take = min(lot.remaining_quantity, to_consume)
        if take <= 0:
            continue
        allocations.append((lot, take))
        # Accumulate unrounded: rounding the running total on every iteration
        # (as the pre-extraction orchestrator code did) compounds rounding
        # error into the weighted average. Round once, at the end, instead.
        total_cost += _money(lot.unit_cost) * take
        to_consume -= take

    if to_consume > 0:
        raise InsufficientLots(requested=quantity, available=quantity - to_consume)

    return allocations, _money(total_cost / quantity)


def check_availability(
    lots: list[Lot], stock_actual: Decimal, requested: Decimal
) -> tuple[bool, Optional[str]]:
    """Paso previo obligatorio a registrar una venta.

    El caso que importa es stock sin lotes: existencia que nunca se migro a
    lotes.  Ahi no hay costo que aplicar, y inventarlo contaminaria el FIFO.
    """
    total = sum((lot.remaining_quantity for lot in lots), Decimal("0"))

    if stock_actual > 0 and total <= 0:
        return False, (
            f"El producto tiene stock ({stock_actual}) pero esta sin lotes. "
            "Crea el lote correspondiente antes de vender; no se puede inventar un costo."
        )
    if total < requested:
        return False, f"Lotes insuficientes: disponible={total}, pedido={requested}"
    return True, None
