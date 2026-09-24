from decimal import ROUND_HALF_UP, Decimal

from app.schemas.sale import ProfitReportRow

MONEY = Decimal("0.01")
ALLOWED_GROUP_BY = ("sale", "customer", "product")


class InvalidGroupBy(Exception):
    """El group_by pedido no es uno de los soportados.

    Es un error de dominio, no HTTP: el orquestador lo traduce a 422 para la
    API, y el agente lo recibe como excepcion normal.
    """

    def __init__(self, group_by: str, allowed: tuple[str, ...] = ALLOWED_GROUP_BY):
        self.group_by = group_by
        self.allowed = allowed
        super().__init__(f"group_by must be one of: {', '.join(allowed)}")


def _money(value: Decimal | float | int | None) -> Decimal:
    if value is None:
        return Decimal("0.00")
    return Decimal(str(value)).quantize(MONEY, rounding=ROUND_HALF_UP)


def build_profit_rows(sales, group_by: str = "product") -> list[ProfitReportRow]:
    """Agrega ingreso y ganancia por sale/customer/product.

    `quantity` es Decimal, no int: el negocio vende medios cartones y sumarlos
    en un entero fue lo que devolvia 500 en el reporte.

    Lee `item.gross_profit_total`, que ya viene calculado en la venta; no
    recalcula margenes.
    """
    rows: dict[str, ProfitReportRow] = {}

    for sale in sales:
        for item in sale.items:
            if group_by == "sale":
                key = str(sale.id)
                label = f"{sale.date} - {sale.customer.name if sale.customer else 'Unknown'}"
            elif group_by == "customer":
                key = str(sale.customer_id)
                label = sale.customer.name if sale.customer else "Unknown"
            elif group_by == "product":
                key = str(item.product_id)
                label = item.product.name if item.product else "Unknown"
            else:
                raise InvalidGroupBy(group_by)

            if key not in rows:
                rows[key] = ProfitReportRow(
                    key=key,
                    label=label,
                    quantity=Decimal("0"),
                    revenue=Decimal("0.00"),
                    gross_profit=Decimal("0.00"),
                )

            rows[key].quantity += Decimal(str(item.quantity))
            rows[key].revenue = _money(rows[key].revenue + _money(item.subtotal))
            rows[key].gross_profit = _money(
                rows[key].gross_profit + _money(item.gross_profit_total)
            )

    return list(rows.values())
