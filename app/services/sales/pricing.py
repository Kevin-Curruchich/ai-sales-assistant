from decimal import ROUND_HALF_UP, Decimal
from typing import Optional

MONEY = Decimal("0.01")
HUNDRED = Decimal("100")
DEFAULT_TOLERANCE = Decimal("2")
MIN_SALES_FOR_PATTERN = 3


def _money(value: Decimal | float | int | None) -> Decimal:
    if value is None:
        return Decimal("0.00")
    return Decimal(str(value)).quantize(MONEY, rounding=ROUND_HALF_UP)


def base_margin_for(product, cost_basis: Decimal) -> Decimal:
    """El margen estandar del producto, en quetzales por unidad."""
    mode = getattr(product.earning_mode, "value", product.earning_mode)
    if mode == "percent":
        percent = Decimal(str(product.earning_percent or 0))
        return _money(cost_basis * (percent / HUNDRED))
    return _money(product.earning_fee_amount or 0)


def suggested_unit_price(product, cost_basis: Decimal) -> Decimal:
    return _money(cost_basis + base_margin_for(product, cost_basis))


def margins(
    unit_price: Decimal, cost_basis: Decimal, base_margin: Decimal
) -> tuple[Decimal, Decimal]:
    """margen_real = precio - costo; margen_extra = real - base."""
    real = _money(unit_price - cost_basis)
    return real, _money(real - base_margin)


def detect_habitual_margin(
    observed_margins: list[Decimal],
    base_margin: Decimal,
    tolerance: Decimal = DEFAULT_TOLERANCE,
) -> Optional[Decimal]:
    """El margen habitual del cliente, si lo tiene.

    Con menos de tres ventas no hay patron suficiente.  Si los tres margenes
    caben dentro de `tolerance` entre si pero difieren del estandar, ese es el
    precio habitual del cliente.

    Una rebaja puntual no reescribe el patron: rompe la consistencia entre los
    tres y el resultado es None, que es exactamente lo que pide la skill.

    `tolerance` mide solo la consistencia entre las tres ventas.  La comparacion
    contra el estandar es exacta.
    """
    if len(observed_margins) < MIN_SALES_FOR_PATTERN:
        return None

    recent = [_money(m) for m in observed_margins[:MIN_SALES_FOR_PATTERN]]
    if max(recent) - min(recent) > tolerance:
        return None

    habitual = _money(sum(recent) / len(recent))
    # Distinto del estandar significa distinto, no "distinto por mas de la
    # tolerancia".  Usar aqui los mismos Q2 que miden la consistencia entre si
    # se tragaria el caso que motiva esta funcion: un cliente que paga Q36 donde
    # el estandar son Q37 tiene un margen habitual de 2.50 contra 3.50 — una
    # diferencia de Q1 que quedaria dentro de la tolerancia y nunca se detectaria.
    if habitual == _money(base_margin):
        return None
    return habitual
