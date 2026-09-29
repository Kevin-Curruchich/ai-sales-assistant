"""Herramientas de escritura.  Las tres se detienen antes de tocar la base.

`interrupt()` NO congela un frame de Python vivo.  Al reanudar con
`Command(resume=...)`, LangGraph vuelve a correr la funcion ENTERA desde el
principio -- el valor de resume se empareja por indice de llamada a
`interrupt()`, no por continuar la ejecucion donde quedo.  Verificado en este
venv con un `InMemorySaver` real: un nodo que hace `x = state["x"] + n` antes
de un `interrupt()` calcula `x` DE NUEVO, con datos frescos, en la reanudacion
-- no reutiliza el valor que le mostro a la persona.

Esto tiene una consecuencia que la primera version de este archivo no
resolvia bien: una linea como

    preview = service.preview_sale(data)   # antes del interrupt()
    decision = interrupt(...)
    recalculado = service.preview_sale(data)   # despues

en una reanudacion REAL, calcula `preview` Y `recalculado` los dos DESPUES de
la pausa, microsegundos aparte, sobre el mismo estado de la base -- nunca
sobre lo que la persona efectivamente vio antes de aprobar.  Comparar uno
contra otro no prueba nada; la comparacion es estructuralmente siempre
verdadera.  (Un stand-in de `interrupt()` de una sola pasada, como el que usan
los tests con monkeypatch, no expone esto: ese stand-in SI modela una
ejecucion que atraviesa la pausa, la real no.)

Lo unico que cruza la pausa intacto es el valor que trae el resume.  Por eso
cada escritura arma una HUELLA (`cost_basis_unit`/`subtotal`/lotes/warnings
para una venta; subtotal/estado de producto/total para una compra) y la mete
DENTRO del payload de `interrupt()`: viaja al panel, se congela en el
checkpoint, y el panel la devuelve tal cual dentro de la aprobacion
(`{"accion": "aprobar", "huella": ...}`).  Solo esa huella devuelta -- nunca
un recalculo local hecho en esta misma ejecucion -- se compara contra un
recalculo fresco hecho DESPUES de la reanudacion.  Si difiere, no se escribe
-- se avisa con `"estado": "recalculado"`.  Si la aprobacion no trae una
huella utilizable (ausente, `None`, o vacia -- por ejemplo un panel armado
contra el contrato de la ronda 0, que no mandaba huella), se avisa distinto,
con `"estado": "aprobacion_sin_huella"`: sin esta distincion, ese panel viejo
recibiria "recalculado" ("el inventario cambio") para SIEMPRE, en cada
aprobacion, sin ninguna pista de que el problema es el contrato y no el
inventario. Esto cambia el contrato del panel (tiene que guardar y devolver
`huella` sin tocarla); Task 10 lo documenta. La huella es un valor sin firmar
que el cliente retiene entre la pausa y el resume: un panel que la
RECALCULA en vez de echoarla derrota la guardia en silencio (siempre
"coincide" con lo que el mismo panel acaba de calcular) -- esta herramienta
no puede distinguir eso de un echo honesto cuando nada cambio en el medio;
solo puede decir lo que SI puede distinguir, que es la ausencia total de
huella.

`db.expire_all()` antes de cada recalculo NO es lo que hace funcionar esta
guardia bajo una reanudacion real -- `agent_session()` abre una sesion NUEVA
en cada reanudacion (ver mas abajo), asi que el identity map de SQLAlchemy
arranca VACIO cuando el recalculo corre, y expirarlo ahi no cambia nada en
ese caso. Lo que SI protege: un escritor genuinamente CONCURRENTE -- otra
conexion, otra sesion -- que comitea ENTRE el preview descartable de esta
ejecucion y el recalculo, los dos dentro de la MISMA sesion de esta misma
ejecucion. Sin expirar, esa segunda lectura podria devolver los mismos
objetos Python que la primera ya cargo, ignorando el commit ajeno. Se
mantiene la llamada por eso -- no porque sea lo que hace que la comparacion
contra la huella funcione; eso lo hace la huella misma.

Sobre `corregir`: usa un `while` en vez de que la herramienta se reinvoque a
si misma via `.invoke()`.  La razon original que se dio para esto (que
`.invoke({...})` sin `config=` explicito pierde el `user_id`) resulto ser
FALSA -- verificado en este venv: un `.invoke()` hecho desde DENTRO de la
ejecucion de otro tool SI hereda el `RunnableConfig` del padre via el
contextvar que LangChain propaga (`context.run(...)` en `BaseTool.run`).  La
razon real para preferir el loop es otra: es el patron que la documentacion
de LangGraph muestra para "pedir de nuevo" dentro de una sola ejecucion en
linea recta, no crece el stack por cada correccion, y no puede girar sin
control porque cada vuelta bloquea en una decision humana real.

Limite conocido, FUERA del alcance de este archivo -- Task 9 lo hereda como
condicion de aceptacion BLOQUEANTE: `ToolNode` corre todas las tool calls de
un mismo mensaje del modelo como UNA sola tarea. Si una escritura de este
archivo ya se completo y comiteo, y una HERMANA en el mismo paso todavia
esta en un `interrupt()`, reanudar ese paso vuelve a correr TODO el paso
desde el principio -- incluida la escritura que ya se habia completado.
Confirmado contra langgraph 1.0.3, con consecuencias CONCRETAS por
herramienta (no un riesgo abstracto):

  - `registrar_compra` duplica la compra, el incremento de stock Y la
    salida de caja -- las tres comiteadas de nuevo.
  - `registrar_movimiento_caja` duplica SIEMPRE: no tiene ninguna guardia
    que pueda distinguir una repeticion de una solicitud nueva.
  - `registrar_venta` escapa solo por ACCIDENTE: si el lote tiene unidades
    de sobra al mismo costo unitario, la huella recalculada en la segunda
    pasada es identica a la primera y la guardia la deja pasar sin
    protestar -- solo corta la repeticion cuando el lote es chico y la
    primera escritura ya lo dejo insuficiente para la segunda.

Nada en este archivo puede distinguir esa repeticion de una solicitud nueva
con los mismos valores sin una clave de idempotencia que viaje por fuera de
estas funciones. Esa clave ahora existe: `app/agent/idempotency.py` la saca
del `config` (el id de la tarea de Pregel, estable a traves de la
reanudacion y distinto por tool_call hermana) y la anota en
`agent.tool_writes` DENTRO de la misma transaccion que la escritura del
negocio. Las tres herramientas la consultan antes de pausar y devuelven
`"estado": "ya_registrado"` si esta tarea ya escribio.

Eso cubre la ventana que este docstring describe Y la otra, mas grave: un
proceso que muere DESPUES del commit y ANTES de que LangGraph anote el
resultado de la tarea en el checkpoint.

`registrar_compra` escribe en DOS transacciones (`create()` comitea el
borrador, `confirm()` comitea el stock y la salida de caja) y la marca
viaja con la SEGUNDA -- ver el comentario en su cuerpo. Lo que queda: una
muerte ENTRE las dos deja un borrador huerfano, sin confirmar, que nadie
vuelve a mirar; la reanudacion no lo reusa, crea uno nuevo y lo confirma.
Ni compra duplicada ni caja duplicada -- un borrador de mas que hay que
borrar desde el panel. Anotado en `docs/agente.md`, seccion
"Idempotencia".
"""

import uuid
from datetime import date as date_type
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
from langgraph.types import interrupt

from app.agent import idempotency
from app.agent.session import agent_session, user_id_from_config
from app.agent.signing import firmar, verificar
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


def _money(value) -> Decimal:
    """Mismo redondeo que `SaleService._money`/`PurchaseService._money`:
    ROUND_HALF_UP, no el ROUND_HALF_EVEN que trae `Decimal.quantize()` por
    default.  En cantidades fraccionarias los dos criterios desacuerdan por
    un centavo -- exactamente el desacuerdo entre preview y create() que
    esta task existe para prevenir (Finding I2 de la revision: sin esto,
    `_purchase_preview` podia mostrar Q3.12 mientras la base escribia Q3.13,
    y la huella los veria "iguales" porque los dos lados de la comparacion
    usaban el redondeo equivocado)."""
    return Decimal(str(value)).quantize(MONEY, rounding=ROUND_HALF_UP)


def _huella_ausente(huella) -> bool:
    """True si `huella` no sirve para comparar: ausente, `None`, o vacia
    (`[]`/`{}`).  Distingue "el panel no mando huella" de "la huella no
    coincide con el recalculo" -- lo primero es un problema de contrato, no
    de inventario, y merece un estado propio en vez de "recalculado" con un
    mensaje que sugiere que algo cambio en la base (ronda 2 de revision)."""
    return not huella



def _ya_escrito(db, clave: str | None, herramienta: str, id_key: str) -> dict | None:
    """El resultado a devolver si esta tarea YA escribio, o None.

    `clave is None` significa que no hay grafo detras (invocacion directa):
    no hay reanudacion posible y no hay nada que desduplicar.
    """
    if clave is None:
        return None
    idempotency.ensure_table(db)
    previo = idempotency.find(db, clave)
    if previo is None:
        return None
    return {
        "estado": "ya_registrado",
        id_key: previo["entity_id"],
        "mensaje": (
            f"Esta misma tarea ya ejecuto {herramienta} y la escritura quedo "
            "comiteada. No se volvio a escribir: el proceso se reanudo despues "
            "de que la escritura entrara a la base pero antes de que LangGraph "
            "anotara que la tarea habia terminado."
        ),
    }


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
                # `is not None`, no una verdad booleana: un precio de cero
                # (un regalo) es falsy cuando llega como NUMERO -- `0` /
                # `0.0`, que es lo que el modelo emite cuando el JSON de la
                # tool call no lo manda entre comillas (la cadena `"0"` no es
                # falsy, y por ahi el bug no se veia). Con `i.get(...)` a
                # secas ese cero se leeria como "no vino precio", usando el
                # sugerido a precio completo en vez del cero que se pidio
                # (Minor de la revision).
                unitPrice=Decimal(str(i["precio_unitario"])) if i.get("precio_unitario") is not None else None,
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


def _sale_written_dict(sale_response) -> dict:
    """A diferencia de `_sale_preview_dict`, esto parte de lo que
    EFECTIVAMENTE quedo escrito (`SaleService.create_enriched()`, que relee
    la venta despues de guardarla) y no de una prediccion: `create()`
    resuelve la asignacion FIFO con `lock_for_update=True`, que en teoria
    puede diferir de lo que un preview sin lock calculo un instante antes.
    Devolver la lectura real, no la prediccion, es lo unico honesto que se
    puede reportar como "esto es lo que se registro" (Finding I7).  Va bajo
    la clave "venta" en la respuesta final, no "preview": son dos schemas
    distintos (una lectura real contra una prediccion) y compartir el
    nombre invitaba a tratarlos como si fueran lo mismo."""
    result = sale_response.model_dump(mode="json")
    for item in result["items"]:
        item["lotes"] = item.pop("allocations")
    return result


def _sale_cost_huella(preview: SalePreviewResponse) -> list[dict]:
    """La huella que cruza la pausa: lo unico que se compara al aprobar.

    No alcanza con `cost_basis_unit`/`subtotal`: si otra venta se lleva PARTE
    de un lote (no todo), el costo unitario del lote no cambia -- sigue
    siendo el mismo `unit_cost` de siempre -- y `subtotal` se calcula sobre
    la cantidad PEDIDA, no la disponible, asi que tampoco cambia. Lo que SI
    cambia es cuanto cubre cada lote (`lotes`) y si aparece una advertencia
    de insuficiencia (`warnings`). Sin esos dos campos, una venta con un lote
    parcialmente consumido pasaba esta guardia y `create()` (que si bloquea
    la fila y ve la cantidad real) la rechazaba con una `HTTPException` sin
    manejar, filtrando un error de FastAPI hacia afuera del proceso del
    agente (Finding I1)."""
    return [
        {
            "cost_basis_unit": str(item.cost_basis_unit),
            "subtotal": str(item.subtotal),
            "lotes": [
                {
                    "purchase_item_id": str(a.purchase_item_id),
                    "quantity_taken": str(a.quantity_taken),
                }
                for a in item.allocations
            ],
            "warnings": list(item.warnings),
        }
        for item in preview.items
    ]


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

    El panel DEBE devolver la aprobacion como {"accion": "aprobar", "huella":
    <la huella que vino en el payload de interrupt()>} -- sin tocarla. Esta
    herramienta la usa para confirmar que el inventario no cambio entre que
    se mostro el precio y que se aprobo.

    El resultado trae "estado": "registrado" | "cancelado" | "recalculado" |
    "aprobacion_sin_huella" | "ya_registrado". "registrado" trae la venta
    escrita bajo la clave "venta" (no "preview"). "ya_registrado" significa
    que esta misma tarea ya escribio esta venta y el proceso se reanudo
    despues -- no se escribio de nuevo, no hay nada que corregir.
    "recalculado" significa que el inventario cambio entre que se mostro el
    precio y que se aprobo -- nada se escribio, hay que volver a
    previsualizar. "aprobacion_sin_huella"
    significa que la aprobacion no trajo la huella que se le mostro -- un
    problema del panel, no del inventario.
    """
    # Autenticar ANTES de interrumpir: pedirle a una persona que apruebe algo
    # que nunca se iba a poder escribir es el orden equivocado, aunque nada
    # se escriba (no era un bug de correccion, era un orden raro -- ronda 2
    # de revision). El `user_id` lo puso el SERVIDOR en el `configurable` de
    # la corrida, resolviendo el token de Firebase que mando el panel
    # (`app/agent/auth_hook.py`, bajo una clave que el validador del servidor
    # no deja mandar al llamador); `config` lo inyecta LangGraph, el modelo
    # no lo ve ni lo puede inventar.
    usuario_id = user_id_from_config(config)
    clave_escritura = idempotency.write_key(config)

    valores = {
        "cliente_id": cliente_id,
        "items": items,
        "fecha": fecha,
        "medio_pago": medio_pago,
        "pago_pendiente": pago_pendiente,
    }

    with agent_session() as db:
        # ANTES de interrumpir: si esta tarea ya escribio (ver
        # `app/agent/idempotency.py`), no tiene sentido volver a pausar a una
        # persona para que apruebe algo que ya esta en la base. Chequear
        # despues del interrupt tampoco alcanzaria: la venta anterior ya
        # consumio el lote, asi que el recalculo daria distinto de la huella
        # aprobada y la herramienta contestaria "recalculado" ("el inventario
        # cambio") -- cierto pero engañoso, porque quien lo cambio fue esta
        # misma tarea al escribir.
        ya = _ya_escrito(db, clave_escritura, "registrar_venta", "venta_id")
        if ya is not None:
            return ya

        service = SaleService(db)

        while True:
            data = _build_sale_create(**valores)
            preview = service.preview_sale(data)
            huella = firmar(_sale_cost_huella(preview))

            decision = interrupt(
                {
                    "tipo": "confirmar_venta",
                    "preview": _sale_preview_dict(preview),
                    "huella": huella,
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

            huella_aprobada = decision.get("huella")
            if _huella_ausente(huella_aprobada):
                return {
                    "estado": "aprobacion_sin_huella",
                    "mensaje": (
                        "La aprobacion no trajo la huella que se le mostro en el "
                        "payload de interrupt() -- el panel debe devolverla tal "
                        "cual, sin recalcularla ni omitirla. No se escribio nada; "
                        "hay que volver a previsualizar y aprobar de nuevo."
                    ),
                }

            # `preview`/`huella` de ARRIBA son de esta ejecucion del nodo. En
            # una reanudacion real, LangGraph corrio esta funcion entera de
            # nuevo desde el principio -- esas dos lineas se calcularon DESPUES
            # de la pausa, con datos frescos, no son "lo que la persona vio".
            # Lo unico que si lo es: `huella_aprobada`, que viajo dentro del
            # payload de interrupt(), quedo congelada en el checkpoint, y el
            # panel la devolvio tal cual al aprobar.
            #
            # `db.expire_all()`: protege contra un escritor CONCURRENTE que
            # comitee entre esta linea y la de abajo, dentro de la MISMA
            # sesion -- no es lo que hace que la comparacion contra la huella
            # funcione bajo una reanudacion real (ver el docstring del modulo).
            # La firma prueba que esta huella la emitio ESTE servidor.  Sin
            # esto, un panel que la recalcule en vez de guardarla apaga la
            # comparacion en silencio y nadie se entera.
            firma_ok, cifras_aprobadas = verificar(huella_aprobada)
            if not firma_ok:
                return {
                    "estado": "huella_no_valida",
                    "mensaje": (
                        "La huella de la aprobacion no fue emitida por este "
                        "servidor. El panel debe devolver TAL CUAL el valor que "
                        "vino en el payload de interrupt(), sin recalcularlo ni "
                        "modificarlo. No se escribio nada."
                    ),
                }

            db.expire_all()
            recalculado = service.preview_sale(data)
            if cifras_aprobadas != _sale_cost_huella(recalculado):
                return {
                    "estado": "recalculado",
                    "mensaje": "El inventario cambio y el costo difiere de lo que aprobaste.",
                    "huella_aprobada": cifras_aprobadas,
                    "huella_actual": _sale_cost_huella(recalculado),
                    "preview_actual": _sale_preview_dict(recalculado),
                }

            # Idempotencia: la marca entra en la MISMA transaccion que la
            # venta (misma sesion, sin commit propio), asi que o quedan las
            # dos o no queda ninguna. Ver `app/agent/idempotency.py`.
            if clave_escritura is not None:
                idempotency.claim(db, clave_escritura, "registrar_venta")

            enriched = service.create_enriched(data, user_id=usuario_id)
            if clave_escritura is not None:
                idempotency.record_entity(db, clave_escritura, str(enriched.id))
            return {
                "estado": "registrado",
                "venta_id": str(enriched.id),
                "venta": _sale_written_dict(enriched),
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
    y la aprobacion es el estado del producto (si se desactivo o se borro
    mientras tanto) o el costo/cantidad si se corrigio -- ambos se recalculan
    igual.
    """
    product_repo = ProductRepository(db)
    items_preview = []
    total = Decimal("0.00")
    for item in data.items:
        product = product_repo.get_by_id(item.productId)
        subtotal = _money(item.unitCost * item.quantity)
        total = _money(total + subtotal)
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


def _purchase_huella(preview: dict) -> dict:
    """Lo unico que se compara al aprobar una compra: subtotal por item,
    si el producto sigue activo/existente, y el total. Igual que en la venta,
    esto viaja DENTRO del payload de interrupt() y el panel lo devuelve tal
    cual al aprobar -- no se recalcula localmente para comparar contra si
    mismo (Finding C1)."""
    return {
        "total": preview["total"],
        "items": [
            {
                "subtotal": i["subtotal"],
                "producto_activo": i["producto_activo"],
                "producto_existe": i["producto_existe"],
            }
            for i in preview["items"]
        ],
    }


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

    El panel DEBE devolver la aprobacion como {"accion": "aprobar", "huella":
    <la huella que vino en el payload de interrupt()>} -- sin tocarla.

    Confirma de inmediato -- no deja un borrador colgado: un borrador sin
    confirmar no libera lotes FIFO ni mueve caja, asi que dejarlo a medias
    seria peor que no haber hecho nada. Si el socio puso el dinero para esta
    compra, preguntale al usuario y registralo aparte con
    registrar_movimiento_caja (tipo="aporte_socio", compra_id=<esta compra>)
    -- esta herramienta nunca lo hace por su cuenta.

    El resultado trae "estado": "registrado" | "cancelado" | "recalculado" |
    "aprobacion_sin_huella" | "ya_registrado". "registrado" trae la compra
    confirmada bajo la clave "compra" (no "preview"). "ya_registrado"
    significa que esta misma tarea ya la escribio y el proceso se reanudo
    despues -- no se escribio de nuevo.
    """
    # Autenticar ANTES de interrumpir -- ver la nota identica en
    # registrar_venta (ronda 2 de revision).
    usuario_id = user_id_from_config(config)
    clave_escritura = idempotency.write_key(config)

    valores = {
        "items": items,
        "fecha": fecha,
        "proveedor": proveedor,
        "referencia": referencia,
        "medio_pago": medio_pago,
        "notas": notas,
    }

    with agent_session() as db:
        # Ver la nota identica en registrar_venta.
        ya = _ya_escrito(db, clave_escritura, "registrar_compra", "compra_id")
        if ya is not None:
            return ya

        while True:
            data = _build_purchase_create(**valores)
            preview = _purchase_preview(db, data)
            huella = firmar(_purchase_huella(preview))

            decision = interrupt(
                {
                    "tipo": "confirmar_compra",
                    "preview": preview,
                    "huella": huella,
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

            huella_aprobada = decision.get("huella")
            if _huella_ausente(huella_aprobada):
                return {
                    "estado": "aprobacion_sin_huella",
                    "mensaje": (
                        "La aprobacion no trajo la huella que se le mostro en el "
                        "payload de interrupt() -- el panel debe devolverla tal "
                        "cual, sin recalcularla ni omitirla. No se escribio nada; "
                        "hay que volver a previsualizar y aprobar de nuevo."
                    ),
                }

            firma_ok, cifras_aprobadas = verificar(huella_aprobada)
            if not firma_ok:
                return {
                    "estado": "huella_no_valida",
                    "mensaje": (
                        "La huella de la aprobacion no fue emitida por este "
                        "servidor. El panel debe devolver TAL CUAL el valor que "
                        "vino en el payload de interrupt(), sin recalcularlo ni "
                        "modificarlo. No se escribio nada."
                    ),
                }

            db.expire_all()  # ver la nota identica en registrar_venta (Finding I4)
            recalculado = _purchase_preview(db, data)
            if cifras_aprobadas != _purchase_huella(recalculado):
                return {
                    "estado": "recalculado",
                    "mensaje": "Los datos de la compra cambiaron y difieren de lo que aprobaste.",
                    "huella_aprobada": cifras_aprobadas,
                    "huella_actual": _purchase_huella(recalculado),
                    "preview_actual": recalculado,
                }

            service = PurchaseService(db)
            purchase = service.create(data, user_id=usuario_id)
            try:
                # La marca va DESPUES de `create()` y ANTES de `confirm()`, a
                # proposito: esta herramienta escribe en DOS transacciones y
                # la marca tiene que viajar con la SEGUNDA.
                #
                # `PurchaseRepository.create` comitea. Con la marca anotada
                # antes de `service.create()`, se comiteaba junto con el
                # borrador -- y si `confirm()` despues reventaba, el manejador
                # de abajo hacia rollback y borraba el borrador, pero la marca
                # ya era durable. No quedaba nada escrito y el reintento
                # contestaba "ya_registrado", que `docs/agente.md` le dice al
                # panel que trate como exito: un exito que nunca ocurrio. Y
                # `confirm()` reventando no es raro -- un 409 por un producto
                # desactivado, un 404 por uno que desaparecio, un error de
                # base. Puesta aca, la comitea el `db.commit()` de `confirm()`
                # y el rollback del manejador se la lleva con todo lo demas.
                if clave_escritura is not None:
                    idempotency.claim(db, clave_escritura, "registrar_compra")
                confirmed = service.confirm(purchase.id)
            except Exception:
                # Un borrador que el agente deja colgado es peor que no
                # haberlo creado -- pero sin el rollback de aca, borrarlo es
                # peor todavia (Finding C2):
                #
                # 1. Si confirm() fallo A MITAD del loop de items, con el
                #    stock de un item anterior ya incrementado EN LA SESION
                #    (sin commitear): PurchaseRepository.delete() hace
                #    `db.commit()` al borrar el borrador, y ESE commit se
                #    lleva puesto el incremento de stock a medias -- stock
                #    fantasma, sin lote ni compra que lo explique.
                # 2. Si confirm() fallo DESPUES de poner
                #    `purchase.status = "confirmed"` en la sesion (tambien
                #    sin commitear): delete() lee ese estado sin commitear,
                #    ve "confirmed" y rechaza con 409 -- el borrador queda
                #    colgado Y el error real queda tapado por el 409.
                #
                # `db.rollback()` deja la sesion exactamente como esta
                # commiteada de verdad (solo el draft, sin tocar stock) antes
                # de borrar. El error de limpieza nunca reemplaza al
                # original: se traga aparte y se relanza el de confirm().
                db.rollback()
                try:
                    service.delete(purchase.id)
                except Exception:
                    pass
                raise

            if clave_escritura is not None:
                idempotency.record_entity(db, clave_escritura, str(confirmed.id))
            return {
                "estado": "registrado",
                "compra_id": str(confirmed.id),
                "compra": confirmed.model_dump(mode="json"),
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
    "estado": "registrado" | "cancelado" | "ya_registrado" (esta misma tarea
    ya lo escribio y el proceso se reanudo despues; no se escribio de
    nuevo).
    """
    # `CashMovement` no tiene columna `user_id` -- esta llamada no se usa
    # para asociar el movimiento a nadie, sino para comprobar que el
    # SERVIDOR autentico esta corrida (`app/agent/auth_hook.py`), y se hace
    # ANTES de interrumpir: sin ella, un hilo sin autenticar podia
    # interrumpir a una persona, juntar una aprobacion, y recien ahi
    # reventar -- pedirle a alguien que apruebe algo que nunca se iba a
    # poder escribir es el orden equivocado (ronda 2 de revision; antes
    # esta llamada estaba al final, justo antes de escribir).
    user_id_from_config(config)
    clave_escritura = idempotency.write_key(config)

    if clave_escritura is not None:
        # Sesion propia y corta: a diferencia de las otras dos herramientas,
        # esta no mantiene una sesion abierta a lo largo del loop (no tiene
        # nada que recalcular). Ver la nota de registrar_venta sobre por que
        # el chequeo va ANTES de interrumpir.
        with agent_session() as db:
            ya = _ya_escrito(db, clave_escritura, "registrar_movimiento_caja", "movimiento_id")
        if ya is not None:
            return ya

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
            if clave_escritura is not None:
                idempotency.claim(db, clave_escritura, "registrar_movimiento_caja")

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
            if clave_escritura is not None:
                idempotency.record_entity(db, clave_escritura, str(movement.id))
            return {
                "estado": "registrado",
                "movimiento_id": str(movement.id),
            }
