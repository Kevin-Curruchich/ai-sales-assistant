"""Proyeccion de la proxima compra por par cliente+producto.

EWMA (media movil exponencialmente ponderada) en vez de la media simple: un
hueco de 56 dias que ocurrio una sola vez hace meses no debe pesar igual que
la ultima compra. Con menos de dos fechas distintas de compra NO se proyecta
nada -- inventar un intervalo por defecto es justo lo que el AGENTS.md
prohibe.
"""

from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Optional

ALPHA = Decimal("0.3")
METHOD_EWMA = "ewma"
FOUR_PLACES = Decimal("0.0001")


def ewma_interval(intervals: list[int], alpha: Decimal = ALPHA) -> Decimal:
    """Promedio movil exponencialmente ponderado sobre los intervalos.

    La observacion mas reciente pesa `alpha`; todo lo anterior se reparte el
    resto, decayendo geometricamente. Con alpha=0.3 las ultimas ocho compras
    concentran el 94% del peso, a diferencia de la media simple, donde una
    compra de hace tres meses pesa igual que la de ayer.
    """
    if not intervals:
        raise ValueError("ewma_interval necesita al menos un intervalo")

    estimate = Decimal(intervals[0])
    for observed in intervals[1:]:
        estimate = alpha * Decimal(observed) + (Decimal("1") - alpha) * estimate
    return estimate


def confidence_for(total_purchases: int) -> str:
    if total_purchases < 2:
        return "insufficient"
    if total_purchases <= 3:
        return "low"
    if total_purchases <= 7:
        return "medium"
    return "high"


def project(
    purchase_dates: list[date],
) -> tuple[Optional[Decimal], Optional[date], str, str]:
    """Proyecta la proxima compra a partir de las fechas historicas del par.

    Con menos de dos compras NO proyecta: devuelve None y confianza
    `insufficient`. Inventar un intervalo por defecto es justo lo que el
    AGENTS.md prohibe.
    """
    dates = sorted(set(purchase_dates))
    confidence = confidence_for(len(dates))

    if len(dates) < 2:
        return None, None, METHOD_EWMA, confidence

    intervals = [(dates[i + 1] - dates[i]).days for i in range(len(dates) - 1)]
    # No hace falta un piso de un dia: sorted(set(...)) garantiza fechas
    # distintas, asi que todo intervalo es >= 1 y el EWMA de valores >= 1
    # tambien lo es. Un piso aqui seria una rama que ningun test puede alcanzar.
    estimate = ewma_interval(intervals).quantize(FOUR_PLACES, rounding=ROUND_HALF_UP)
    next_date = dates[-1] + timedelta(days=int(estimate.to_integral_value(ROUND_HALF_UP)))
    return estimate, next_date, METHOD_EWMA, confidence
