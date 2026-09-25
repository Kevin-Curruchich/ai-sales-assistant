"""Herramientas de consulta.  Ninguna escribe.

Todas devuelven dicts planos: un objeto de SQLAlchemy se vuelve inutilizable
cuando la sesion se cierra, y la sesion vive menos que la conversacion.
`Decimal` tampoco es serializable por default -- las respuestas de Pydantic se
convierten con `model_dump(mode="json")`; lo que se arma a mano aca (caja,
seguimiento) convierte cada `Decimal` a `str` explicitamente, para no perder
precision con un `float`.
"""

from datetime import date as date_type
from datetime import datetime
from decimal import Decimal

from langchain_core.tools import tool

from app.agent.session import agent_session
from app.schemas.sale import SaleCreate, SaleItemCreate
from app.services.cash_service import CashService
from app.services.customer_service import CustomerService
from app.services.sales import SaleService


@tool
def buscar_cliente(nombre: str) -> dict:
    """Busca clientes por nombre, empresa o correo.

    Devuelve hasta 10 coincidencias.  Si hay mas de una, pregunta cual antes
    de seguir: nunca elijas por el usuario.  `total` es cuantas coincidencias
    existen en total -- si es mayor que la cantidad de `clientes` devueltos,
    hay mas sin listar (`hay_mas`); nunca lo digas como si "clientes" fuera
    todo lo que hay.
    """
    with agent_session() as db:
        service = CustomerService(db)
        found = service.get_all(search=nombre, limit=10)
        total = service.count(search=nombre)
        return {
            "clientes": [
                {"id": str(c.id), "nombre": c.name, "empresa": c.company, "email": c.email}
                for c in found
            ],
            "total": total,
            "hay_mas": total > len(found),
        }


@tool
def previsualizar_venta(cliente_id: str, items: list[dict], fecha: str) -> dict:
    """Calcula una venta sin registrarla.

    Devuelve, por item: de que lotes sale y cuanto de cada uno (`lotes`), el
    costo unitario (`cost_basis_unit`), el precio sugerido, si ese precio es
    el habitual del cliente (`is_habitual_price`), el margen, y advertencias
    si los lotes no alcanzan.

    Usala SIEMPRE antes de registrar_venta.  `items` es una lista de
    {"producto_id": str, "cantidad": str, "precio_unitario": str opcional}.
    """
    with agent_session() as db:
        data = SaleCreate(
            customerId=cliente_id,
            date=date_type.fromisoformat(fecha),
            items=[
                SaleItemCreate(
                    productId=i["producto_id"],
                    quantity=Decimal(str(i["cantidad"])),
                    unitPrice=Decimal(str(i["precio_unitario"])) if i.get("precio_unitario") else None,
                )
                for i in items
            ],
        )
        preview = SaleService(db).preview_sale(data)
        result = preview.model_dump(mode="json")
        # `allocations` -> `lotes`: el resto de las claves conserva el nombre
        # del schema (SaleItemPreview), este es el unico que la tarjeta de
        # confirmacion necesita en espanol.
        for item in result["items"]:
            item["lotes"] = item.pop("allocations")
        return result


@tool
def consultar_seguimiento(filtro: str = "all", limite: int = 10, offset: int = 0) -> dict:
    """Lista clientes con seguimiento de recompra pendiente.

    `filtro`: "all" | "overdue" | "7_days" | "14_days" | "30_days". Por cada
    cliente devuelve los productos que se espera que necesite, cuantos dias
    faltan (negativo si ya se paso la fecha) y si el stock esta bajo.
    """
    with agent_session() as db:
        seguimientos, total = SaleService(db).get_follow_ups(
            filter_type=filtro, limit=limite, offset=offset
        )
        return {
            "seguimientos": [s.model_dump(mode="json") for s in seguimientos],
            "total": total,
        }


def _parse_boundary(value: str, *, inclusive_end: bool) -> datetime:
    """Convierte "AAAA-MM-DD" (o un timestamp ISO completo) a `datetime`.

    Una fecha sin hora usada como limite SUPERIOR tiene que cubrir el dia
    entero: `datetime.fromisoformat("2026-09-24")` da medianoche, y el
    repositorio filtra `occurred_at <= hasta` -- sin este ajuste, "hasta hoy"
    excluiria todo lo ocurrido hoy mismo despues de las 00:00. El limite
    INFERIOR no tiene ese problema: medianoche ya es el primer instante del
    dia, asi que `occurred_at >= desde` incluye el dia completo tal cual.
    Si `value` ya trae hora, se respeta sin tocarla.
    """
    parsed = datetime.fromisoformat(value)
    is_bare_date = "T" not in value and " " not in value
    if inclusive_end and is_bare_date:
        parsed = parsed.replace(hour=23, minute=59, second=59, microsecond=999999)
    return parsed


@tool
def consultar_caja(desde: str | None = None, hasta: str | None = None, limite: int = 20) -> dict:
    """Consulta el estado de caja: saldo operativo, saldo del socio y el libro de movimientos.

    `saldo` es el efectivo operativo disponible ahora mismo; `saldo_socio` es
    lo que el negocio le debe al socio (aportes menos retiros) -- ambos son
    siempre el acumulado a hoy, sin importar el rango del libro. `desde` y
    `hasta` ("AAAA-MM-DD", opcionales) acotan que movimientos se listan; una
    `hasta` sin hora incluye ese dia completo. `limite` corta cuantos de los
    mas recientes de ese rango se devuelven.
    """
    with agent_session() as db:
        service = CashService(db)
        start = _parse_boundary(desde, inclusive_end=False) if desde else None
        end = _parse_boundary(hasta, inclusive_end=True) if hasta else None
        entries = list(reversed(service.ledger(start=start, end=end)))
        if limite:
            entries = entries[:limite]
        return {
            "saldo": str(service.running_balance()),
            "saldo_socio": str(service.owner_balance()),
            "movimientos": [
                {
                    "id": str(movement.id),
                    "fecha": movement.occurred_at.isoformat(),
                    "tipo": movement.type.value,
                    "monto": str(movement.amount),
                    "medio_pago": movement.payment_method.value if movement.payment_method else None,
                    "venta_id": str(movement.sale_id) if movement.sale_id else None,
                    "compra_id": str(movement.purchase_id) if movement.purchase_id else None,
                    "nota": movement.note,
                    "saldo_acumulado": str(saldo_acumulado),
                }
                for movement, saldo_acumulado in entries
            ],
        }
