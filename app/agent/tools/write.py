"""Herramientas de escritura.  Las tres se detienen antes de tocar la base.

`interrupt()` congela el grafo, guarda el estado en el checkpointer y devuelve
el payload al panel.  Al reanudar, la ejecucion sigue DESDE ACA (la funcion
se vuelve a correr desde el principio hasta el `interrupt()` con el mismo
indice, que ya no vuelve a pausar), no desde el principio de la conversacion.

La propiedad central de este archivo: cada escritura se arma, se muestra
completa via `interrupt()`, y solo al aprobar se RECALCULA el mismo calculo
dentro de la sesion que va a escribir, comparando contra lo que se aprobo.
Si difiere, no se escribe -- se avisa.  `preview_sale` no toma locks de fila
(`lock_for_update=False`); entre el momento en que el panel le muestra el
precio a una persona y el momento en que esa persona aprieta "aprobar" puede
pasar cualquier cantidad de tiempo, y otra venta puede haberse llevado el
mismo lote.  Escribir con el costo viejo seria registrar algo que no paso.

Sobre `corregir` (ver el brief de la Task 8): la version original proponia que
la propia herramienta se reinvocara a si misma via `.invoke()` para rearmar la
operacion.  Eso no se uso aca -- ver la nota debajo de `registrar_venta` con
el razonamiento.
"""

import uuid
from datetime import date as date_type
from datetime import datetime
from decimal import Decimal

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
from langgraph.types import interrupt

from app.agent.session import agent_session, user_id_from_config
from app.core.datetime_utils import business_midnight, business_tz
from app.models.cash_movement import CashMovementType
from app.models.payment_method import PaymentMethod
from app.repositories.product_repository import ProductRepository
from app.schemas.purchase import PurchaseCreate, PurchaseItemCreate
from app.schemas.sale import SaleCreate, SaleItemCreate, SalePreviewResponse
from app.services.cash_service import CashService
from app.services.purchase_service import PurchaseService
from app.services.sales import SaleService

MONEY = Decimal("0.01")


# ---------------------------------------------------------------------
# Venta
# ---------------------------------------------------------------------


def _build_sale_create(
    cliente_id: str,
    items: list[dict],
    fecha: str,
    medio_pago: str,
    pago_pendiente: bool,
) -> SaleCreate:
    return SaleCreate(
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
        isPaymentPending=pago_pendiente,
        # `medio_pago` se ignora si pago_pendiente=True: el validador de
        # SaleCreate lo pone en None, la misma regla que previsualizar_venta
        # y create() ya aplican.
        medioPago=medio_pago,
    )


def _sale_preview_dict(preview: SalePreviewResponse) -> dict:
    """Misma forma que `previsualizar_venta` (Task 7): `allocations` -> `lotes`.

    El panel de confirmacion es el mismo componente para las dos herramientas
    -- reusa la forma en vez de inventar una nueva.
    """
    result = preview.model_dump(mode="json")
    for item in result["items"]:
        item["lotes"] = item.pop("allocations")
    return result


def _same_costs(a: SalePreviewResponse, b: SalePreviewResponse) -> bool:
    """Compara solo lo que la tarjeta de confirmacion mostro: costo unitario
    y subtotal por item.  Si algun otro campo cambia (advertencias, precio
    sugerido) pero estos dos coinciden, lo aprobado sigue siendo verdad."""
    if len(a.items) != len(b.items):
        return False
    return all(
        ia.cost_basis_unit == ib.cost_basis_unit and ia.subtotal == ib.subtotal
        for ia, ib in zip(a.items, b.items)
    )


@tool
def registrar_venta(
    cliente_id: str,
    items: list[dict],
    fecha: str,
    config: RunnableConfig,
    medio_pago: str = "efectivo",
    pago_pendiente: bool = False,
) -> dict:
    """Registra una venta.  Se detiene a pedir confirmacion antes de escribir.

    Llama primero a previsualizar_venta y mostrale al usuario lo que devuelve
    -- esta herramienta vuelve a calcular lo mismo y lo va a mostrar de nuevo
    al pedir la aprobacion final. `items` es una lista de {"producto_id":
    str, "cantidad": str, "precio_unitario": str opcional}.

    El resultado trae "estado": "registrado" | "cancelado" | "recalculado".
    "recalculado" significa que el inventario cambio entre que se mostro el
    precio y que se aprobo -- nada se escribio, hay que volver a previsualizar.
    """
    # `config` lo inyecta LangGraph: el modelo no lo ve ni lo puede inventar.
    # El user_id lo puso el grafo al arrancar la corrida, resolviendo el token
    # de Firebase que mando el panel (Task 6).  La fila la firma esa persona.
    valores = {
        "cliente_id": cliente_id,
        "items": items,
        "fecha": fecha,
        "medio_pago": medio_pago,
        "pago_pendiente": pago_pendiente,
    }

    with agent_session() as db:
        service = SaleService(db)

        # Un `while` en vez de que la herramienta se reinvoque a si misma
        # (`registrar_venta.invoke(...)`) para el caso "corregir".
        #
        # La version del brief hacia justamente eso -- y traia dos problemas
        # que no quise enviar sin resolver:
        #
        # 1. Ese `.invoke({**decision["valores"]})` no reenvia `config`. Un
        #    `@tool` con un parametro `RunnableConfig` lo excluye del schema
        #    e inyecta lo que le llegue por el argumento `config=` de
        #    `.invoke()`; sin ese argumento, LangChain arma un config vacio
        #    (`ensure_config()`), asi que la rama "corregir" perderia el
        #    user_id que la rama normal si tiene. Verificado en este venv
        #    (langchain-core 1.6.5): `tool.invoke({...})` sin `config=` le
        #    llega a la funcion con `configurable={}`.
        #
        # 2. Mas de fondo: no hay garantia documentada de que un segundo
        #    `interrupt()` disparado por una llamada de Python normal (no una
        #    arista nueva del grafo) anidada dentro de la ejecucion que ya
        #    esta siendo REANUDADA se intercale bien con la cola de valores
        #    de resume. `interrupt()` identifica cada pausa por el orden en
        #    que se ejecuta dentro de la MISMA tarea; encadenar llamadas
        #    dentro de una sola ejecucion en linea recta (este `while`) es
        #    exactamente el patron que la documentacion de LangGraph muestra
        #    para "pedir de nuevo" -- ir mas alla de eso, hasta el punto de
        #    apilar invocaciones separadas de la misma herramienta, es
        #    territorio no verificado y no queria enviarlo sin probarlo
        #    contra un checkpointer real, que esta task no monta.
        #
        # Con el loop, "corregir" es simplemente: rearmar los valores y volver
        # a previsualizar y a interrumpir, dentro de la misma invocacion de
        # Python, con el mismo `config` de siempre.
        while True:
            data = _build_sale_create(**valores)
            preview = service.preview_sale(data)

            decision = interrupt(
                {
                    "tipo": "confirmar_venta",
                    "preview": _sale_preview_dict(preview),
                }
            )
            accion = decision.get("accion")

            if accion == "cancelar":
                return {"estado": "cancelado"}

            if accion == "corregir":
                # Corregir no vuelve a la conversacion: rearma y vuelve a
                # confirmar, porque cambiar la cantidad cambia de que lotes
                # sale y cuanto cuesta.
                valores = {**valores, **decision.get("valores", {})}
                continue

            if accion != "aprobar":
                return {
                    "estado": "cancelado",
                    "mensaje": f"Accion no reconocida: {accion!r}. No se escribio nada.",
                }

            # Recalcular DENTRO de la sesion que va a escribir: entre el
            # preview de arriba y esta linea pudo entrar otra venta que
            # consuma los mismos lotes (o el usuario pudo tardarse minutos en
            # aprobar). Misma sesion de base, sin commit de por medio.
            recalculado = service.preview_sale(data)
            if not _same_costs(preview, recalculado):
                return {
                    "estado": "recalculado",
                    "mensaje": "El inventario cambio y el costo difiere de lo que aprobaste.",
                    "anterior": _sale_preview_dict(preview),
                    "actual": _sale_preview_dict(recalculado),
                }

            sale = service.create(data, user_id=user_id_from_config(config))
            return {
                "estado": "registrado",
                "venta_id": str(sale.id),
                "preview": _sale_preview_dict(recalculado),
            }


# ---------------------------------------------------------------------
# Compra
# ---------------------------------------------------------------------


def _build_purchase_create(
    items: list[dict],
    fecha: str,
    proveedor: str | None,
    referencia: str | None,
    medio_pago: str,
    notas: str | None,
) -> PurchaseCreate:
    return PurchaseCreate(
        supplierName=proveedor,
        referenceNumber=referencia,
        date=date_type.fromisoformat(fecha),
        notes=notas,
        items=[
            PurchaseItemCreate(
                productId=i["producto_id"],
                quantity=Decimal(str(i["cantidad"])),
                unitCost=Decimal(str(i["costo_unitario"])),
            )
            for i in items
        ],
        medioPago=medio_pago,
    )


def _purchase_preview(db, data: PurchaseCreate) -> dict:
    """Arma la vista previa de una compra sin escribir nada.

    A diferencia de una venta, el costo de una compra no depende de lotes
    FIFO -- lo dice quien compra. Lo unico que puede cambiar entre el preview
    y la aprobacion es el estado del producto (si se desactivo mientras
    tanto) o el costo/cantidad si se corrigio -- ambos se recalculan igual.
    """
    product_repo = ProductRepository(db)
    items_preview = []
    total = Decimal("0.00")
    for item in data.items:
        product = product_repo.get_by_id(item.productId)
        subtotal = (item.unitCost * item.quantity).quantize(MONEY)
        total = (total + subtotal).quantize(MONEY)
        items_preview.append(
            {
                "producto_id": str(item.productId),
                "producto_nombre": product.name if product else None,
                "producto_sku": product.sku if product else None,
                "producto_activo": bool(product and product.status == "active"),
                "producto_existe": product is not None,
                "cantidad": str(item.quantity),
                "costo_unitario": str(item.unitCost),
                "subtotal": str(subtotal),
            }
        )
    return {
        "proveedor": data.supplierName,
        "referencia": data.referenceNumber,
        "fecha": data.date.isoformat(),
        "medio_pago": data.medioPago.value if data.medioPago else None,
        "notas": data.notes,
        "items": items_preview,
        "total": str(total),
    }


def _same_purchase_totals(a: dict, b: dict) -> bool:
    """Compara solo las cifras y el estado de producto que la tarjeta mostro:
    subtotal por item, si el producto sigue activo/existente, y el total."""
    if len(a["items"]) != len(b["items"]):
        return False
    if a["total"] != b["total"]:
        return False
    return all(
        ia["subtotal"] == ib["subtotal"]
        and ia["producto_activo"] == ib["producto_activo"]
        and ia["producto_existe"] == ib["producto_existe"]
        for ia, ib in zip(a["items"], b["items"])
    )


@tool
def registrar_compra(
    items: list[dict],
    fecha: str,
    config: RunnableConfig,
    proveedor: str | None = None,
    referencia: str | None = None,
    medio_pago: str = "efectivo",
    notas: str | None = None,
) -> dict:
    """Registra una compra y la confirma en la misma operacion.

    Se detiene a pedir confirmacion antes de escribir. `items` es una lista
    de {"producto_id": str, "cantidad": str, "costo_unitario": str}.

    Confirma de inmediato -- no deja un borrador colgado: un borrador sin
    confirmar no libera lotes FIFO ni mueve caja, asi que dejarlo a medias
    seria peor que no haber hecho nada. Si el socio puso el dinero para esta
    compra, preguntale al usuario y registralo aparte con
    registrar_movimiento_caja (tipo="aporte_socio", compra_id=<esta compra>)
    -- esta herramienta nunca lo hace por su cuenta.

    El resultado trae "estado": "registrado" | "cancelado" | "recalculado".
    """
    valores = {
        "items": items,
        "fecha": fecha,
        "proveedor": proveedor,
        "referencia": referencia,
        "medio_pago": medio_pago,
        "notas": notas,
    }

    with agent_session() as db:
        while True:
            data = _build_purchase_create(**valores)
            preview = _purchase_preview(db, data)

            decision = interrupt(
                {
                    "tipo": "confirmar_compra",
                    "preview": preview,
                }
            )
            accion = decision.get("accion")

            if accion == "cancelar":
                return {"estado": "cancelado"}

            if accion == "corregir":
                valores = {**valores, **decision.get("valores", {})}
                continue

            if accion != "aprobar":
                return {
                    "estado": "cancelado",
                    "mensaje": f"Accion no reconocida: {accion!r}. No se escribio nada.",
                }

            recalculado = _purchase_preview(db, data)
            if not _same_purchase_totals(preview, recalculado):
                return {
                    "estado": "recalculado",
                    "mensaje": "Los datos de la compra cambiaron y difieren de lo que aprobaste.",
                    "anterior": preview,
                    "actual": recalculado,
                }

            service = PurchaseService(db)
            purchase = service.create(data, user_id=user_id_from_config(config))
            try:
                confirmed = service.confirm(purchase.id)
            except Exception:
                # Un borrador que el agente deja colgado es peor que no
                # haberlo creado -- si confirmar falla, no queda nada.
                service.delete(purchase.id)
                raise

            return {
                "estado": "registrado",
                "compra_id": str(confirmed.id),
                "preview": recalculado,
            }


# ---------------------------------------------------------------------
# Movimiento de caja
# ---------------------------------------------------------------------


def _occurred_at(value: str) -> datetime:
    """Mismo criterio que `_parse_boundary` (read.py): una fecha sin hora se
    ancla a la medianoche de la zona del negocio, no a UTC ni a un reloj sin
    huso; una hora sin zona se asume tambien en la zona del negocio."""
    is_bare_date = "T" not in value and " " not in value
    if is_bare_date:
        return business_midnight(date_type.fromisoformat(value))
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is not None:
        return parsed
    return parsed.replace(tzinfo=business_tz())


@tool
def registrar_movimiento_caja(
    tipo: str,
    monto: str,
    fecha: str,
    config: RunnableConfig,
    medio_pago: str | None = None,
    compra_id: str | None = None,
    venta_id: str | None = None,
    nota: str | None = None,
) -> dict:
    """Registra un movimiento de caja que NO es una venta ni una compra.

    Una venta o una compra confirmada ya registran su propio movimiento de
    caja (entrada/salida) al crearse -- esta herramienta es para todo lo
    demas: un aporte o retiro del socio, o un saldo inicial. `tipo` es uno de
    "entrada", "salida", "aporte_socio", "retiro_socio", "saldo_inicial".

    Usala para el aporte del socio a una compra (`tipo="aporte_socio"`,
    `compra_id=<id de la compra que financio>`) despues de preguntarle al
    usuario si el socio puso el dinero -- nunca antes ni sin preguntar.

    Se detiene a pedir confirmacion antes de escribir. El resultado trae
    "estado": "registrado" | "cancelado".
    """
    valores = {
        "tipo": tipo,
        "monto": monto,
        "fecha": fecha,
        "medio_pago": medio_pago,
        "compra_id": compra_id,
        "venta_id": venta_id,
        "nota": nota,
    }

    while True:
        decision = interrupt(
            {
                "tipo": "confirmar_movimiento_caja",
                "movimiento": dict(valores),
            }
        )
        accion = decision.get("accion")

        if accion == "cancelar":
            return {"estado": "cancelado"}

        if accion == "corregir":
            valores = {**valores, **decision.get("valores", {})}
            continue

        if accion != "aprobar":
            return {
                "estado": "cancelado",
                "mensaje": f"Accion no reconocida: {accion!r}. No se escribio nada.",
            }

        # No hay nada que recalcular: a diferencia de una venta o una compra,
        # este movimiento no deriva de inventario ni de lotes -- es un hecho
        # que la persona afirma ("el socio puso Q500"), no un calculo que
        # pueda desactualizarse entre el preview y la aprobacion.
        with agent_session() as db:
            service = CashService(db)
            movement = service.record(
                occurred_at=_occurred_at(valores["fecha"]),
                type=CashMovementType(valores["tipo"]),
                amount=Decimal(str(valores["monto"])),
                payment_method=PaymentMethod(valores["medio_pago"]) if valores["medio_pago"] else None,
                sale_id=uuid.UUID(valores["venta_id"]) if valores["venta_id"] else None,
                purchase_id=uuid.UUID(valores["compra_id"]) if valores["compra_id"] else None,
                note=valores["nota"],
                commit=True,
            )
            return {
                "estado": "registrado",
                "movimiento_id": str(movement.id),
            }
