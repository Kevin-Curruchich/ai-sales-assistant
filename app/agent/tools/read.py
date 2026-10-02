"""Herramientas de consulta.  Ninguna escribe.

Todas devuelven dicts planos: un objeto de SQLAlchemy se vuelve inutilizable
cuando la sesion se cierra, y la sesion vive menos que la conversacion.
`Decimal` tampoco es serializable por default -- las respuestas de Pydantic se
convierten con `model_dump(mode="json")`; lo que se arma a mano aca (caja,
seguimiento) convierte cada `Decimal` a `str` explicitamente, para no perder
precision con un `float`.
"""

import uuid
from datetime import date as date_type
from datetime import datetime
from decimal import Decimal

from langchain_core.tools import tool

from app.agent.session import agent_session
from app.core.datetime_utils import business_end_of_day, business_midnight, business_today, business_tz
from app.schemas.sale import SaleCreate, SaleItemCreate
from app.services.cash_service import CashService
from app.services.customer_service import CustomerService
from app.services.product_service import ProductService
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
def buscar_producto(nombre: str) -> dict:
    """Busca productos por nombre o SKU.

    Devuelve hasta 10 coincidencias con su `id` (el UUID que piden
    `previsualizar_venta`, `registrar_venta` y `registrar_compra`), `sku`,
    `nombre`, `precio_sugerido` y `stock`.  Si hay mas de una, pregunta cual
    antes de seguir: nunca elijas por el usuario -- dos productos parecidos
    ("carton de 30" y "carton de 12") terminan en la venta equivocada.

    `total` es cuantas coincidencias existen; si es mayor que las devueltas,
    hay mas sin listar (`hay_mas`), y no digas que `productos` es todo lo que
    hay.

    El `stock` es el contador del producto, no lo que el FIFO puede costear:
    puede haber stock sin lote con costo.  Para saber si una venta se puede
    registrar, usa `previsualizar_venta`.
    """
    with agent_session() as db:
        service = ProductService(db)
        found = service.get_all(search=nombre, limit=10)
        total = service.count(search=nombre)
        return {
            "productos": [
                {
                    "id": str(p.id),
                    "sku": p.sku,
                    "nombre": p.name,
                    "stock": str(p.stock),
                    "min_stock": str(p.min_stock),
                }
                for p in found
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
                    # `is not None`, no una verdad booleana -- la misma
                    # correccion que `_build_sale_create` en write.py. Un
                    # precio de cero (un regalo) es falsy cuando llega como
                    # NUMERO -- `0` / `0.0`, que es lo que el modelo emite
                    # cuando el JSON de la tool call no lo manda entre
                    # comillas; la cadena `"0"` no es falsy y por ahi el bug
                    # no se veia. Con `i.get(...)` a secas ese cero numerico
                    # se leia como "no vino precio" y el preview mostraba el
                    # sugerido a precio completo mientras la escritura, con
                    # la misma entrada, registraba cero. Dos cifras distintas
                    # para la misma venta, y la que el usuario ve antes de
                    # aprobar es la equivocada.
                    unitPrice=Decimal(str(i["precio_unitario"])) if i.get("precio_unitario") is not None else None,
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
    """Convierte "AAAA-MM-DD" (o un timestamp ISO completo) a un `datetime`
    con zona -- `occurred_at` es `timestamptz`, no un reloj sin husario.

    Una fecha sin hora se interpreta en la zona del negocio (Guatemala), no
    UTC ni naive: quien pregunta "desde/hasta el 24" habla del dia calendario
    local, no de un instante que empiece o termine a medianoche UTC. Una
    version anterior de esta funcion devolvia un `datetime` naive, que
    Postgres lee como UTC al compararlo contra `timestamptz` -- el mismo
    desfase de 6 horas que documenta `to_business_tz()`, solo que aca en la
    entrada en vez de la salida:

      - un `hasta` de solo fecha caia a las 17:59:59 hora local (medianoche
        UTC), descartando toda la tarde/noche de ese dia;
      - un `desde` de solo fecha caia a las 18:00 hora local del dia ANTERIOR
        (medianoche UTC), incluyendo de mas esa noche previa.

    `business_midnight()`/`business_end_of_day()` (app/core/datetime_utils)
    construyen el limite correcto para cada lado. Si `value` ya trae una hora
    Y una zona, se usa tal cual -- eso es lo que el llamador pidio. Si trae
    hora pero SIN zona, se asume la zona del negocio en vez de naive/UTC, por
    la misma razon que el caso de solo fecha.
    """
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is not None:
        return parsed

    is_bare_date = "T" not in value and " " not in value
    if is_bare_date:
        day = date_type.fromisoformat(value)
        return business_end_of_day(day) if inclusive_end else business_midnight(day)

    return parsed.replace(tzinfo=business_tz())


@tool
def consultar_caja(desde: str | None = None, hasta: str | None = None, limite: int = 20) -> dict:
    """Consulta el estado de caja: saldo operativo, saldo del socio y el libro de movimientos.

    `saldo` es el efectivo operativo disponible ahora mismo; `saldo_socio` es
    lo que el negocio le debe al socio (aportes menos retiros) -- ambos son
    siempre el acumulado a hoy, sin importar el rango del libro. `desde` y
    `hasta` ("AAAA-MM-DD", opcionales) acotan que movimientos se listan, como
    dias calendario en la zona del negocio (Guatemala): una `hasta` de solo
    fecha incluye ese dia completo hasta medianoche local, no hasta
    medianoche UTC. `limite` corta cuantos de los mas recientes de ese rango
    se devuelven.
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


_ESTADOS_PAGO = {"pendiente": True, "pagada": False}


@tool
def consultar_ventas(
    cliente_id: str | None = None,
    producto_id: str | None = None,
    desde: str | None = None,
    hasta: str | None = None,
    estado_pago: str | None = None,
    limite: int = 10,
    offset: int = 0,
) -> dict:
    """Lista ventas registradas, las mas recientes primero.

    Todos los filtros son opcionales y se combinan. `estado_pago` es
    "pendiente" (a credito, todavia sin cobrar -- las cuentas por cobrar) o
    "pagada"; sin el, trae las dos. `desde`/`hasta` ("AAAA-MM-DD") acotan por
    la fecha de la venta, ambos inclusive. `cliente_id`/`producto_id` son los
    UUID que devuelven `buscar_cliente`/`buscar_producto`.

    `monto_total` y `total` cubren TODAS las ventas del filtro, no solo las
    de esta pagina: para "cuanto me deben" usa `monto_total` con
    `estado_pago="pendiente"`, nunca la suma de las `ventas` listadas.
    `dias_pendiente` es cuantos dias lleva sin cobrarse (solo en las
    pendientes). Para cobrar una, pasale su `id` a `registrar_cobro`.
    """
    if estado_pago is not None and estado_pago not in _ESTADOS_PAGO:
        raise ValueError(
            f"estado_pago debe ser 'pendiente', 'pagada' o vacio, no {estado_pago!r}"
        )
    filtros = {
        "customer_id": uuid.UUID(cliente_id) if cliente_id else None,
        "product_id": uuid.UUID(producto_id) if producto_id else None,
        "start_date": date_type.fromisoformat(desde) if desde else None,
        "end_date": date_type.fromisoformat(hasta) if hasta else None,
        "is_payment_pending": _ESTADOS_PAGO.get(estado_pago) if estado_pago else None,
    }
    hoy = business_today()

    with agent_session() as db:
        service = SaleService(db)
        ventas = service.get_all_enriched(**filtros, limit=limite, offset=offset)
        total = service.count(**filtros)
        return {
            "ventas": [
                {
                    "id": str(v.id),
                    "fecha": v.date.isoformat(),
                    "cliente_id": str(v.customer_id),
                    "cliente": v.customer_name,
                    "total": str(v.total),
                    "pendiente": v.is_payment_pending,
                    "dias_pendiente": (hoy - v.date).days if v.is_payment_pending else None,
                    "fecha_pago": v.payment_date.isoformat() if v.payment_date else None,
                    "medio_pago": v.payment_method.value if v.payment_method else None,
                    "items": [
                        {
                            "producto_id": str(i.product_id),
                            "producto": i.product_name,
                            "cantidad": str(i.quantity),
                            "precio_unitario": str(i.unit_price),
                            "subtotal": str(i.subtotal),
                        }
                        for i in v.items
                    ],
                }
                for v in ventas
            ],
            "total": total,
            "monto_total": str(service.sum_total(**filtros)),
            "hay_mas": offset + len(ventas) < total,
        }
