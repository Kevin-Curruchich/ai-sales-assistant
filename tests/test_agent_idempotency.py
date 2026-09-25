"""Una escritura por tarea, aunque la tarea se re-ejecute.

Las herramientas comitean a Postgres FUERA de la transaccion de LangGraph.
Entre ese commit y el momento en que LangGraph anota el resultado de la
tarea en el checkpoint hay una ventana: un proceso que muera ahi deja un
checkpoint que no sabe que la tarea termino, y reanudar el hilo vuelve a
correr el cuerpo entero -- incluida la escritura ya comiteada.

Estos tests cubren las dos mitades: que la CLAVE sirve (estable a traves de
la reanudacion, distinta por tool_call hermana -- verificado contra un
grafo y un checkpointer reales, no asumido), y que la MARCA hace su trabajo
(la segunda pasada no escribe, la marca comparte transaccion con la
escritura, y dos tareas distintas siguen escribiendo las dos).
"""

import uuid
from datetime import date
from decimal import Decimal

import pytest
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
from langgraph.checkpoint.memory import MemorySaver
from langgraph.prebuilt import create_react_agent
from langgraph.types import Command, interrupt
from sqlalchemy import text

from app.agent import idempotency
from app.agent.session import AUTH_USER_ID_KEY
from app.agent.tools.write import registrar_movimiento_caja, registrar_venta
from app.models.cash_movement import CashMovement
from app.models.sale import Sale
from tests.test_agent_graph import FakeToolCallingModel


def _config(user, task_id, thread="hilo-1"):
    """Lo que una tool call ve: la identidad que inyecto el servidor mas el
    id de la tarea de Pregel que LangGraph pone en cada corrida."""
    return {
        "configurable": {
            AUTH_USER_ID_KEY: str(user.id),
            "thread_id": thread,
            "checkpoint_ns": f"tools:{task_id}",
        }
    }


def _approve(payload):
    return {"accion": "aprobar", "huella": payload.get("huella")}


# ---------------------------------------------------------------------
# La clave: estable a traves de la reanudacion, distinta por hermana
# ---------------------------------------------------------------------


def test_the_write_key_is_stable_across_a_real_resume_and_distinct_per_sibling():
    """Lo unico que hace que esta pieza funcione, verificado y no asumido.

    Un grafo real (`create_react_agent(version="v2")`), un checkpointer
    real, DOS tool_calls en el mismo mensaje del modelo, las dos pausadas en
    `interrupt()`, y una reanudacion de verdad -- que vuelve a ejecutar cada
    tarea desde el principio. La clave que `write_key` saca del `config`
    tiene que ser la MISMA antes y despues de la pausa (si no, la marca no
    sirve de nada) y DISTINTA entre las dos hermanas (si no, la segunda
    escritura legitima del mismo mensaje se saltearia)."""
    vistas: list[tuple[str, str | None]] = []

    @tool
    def sonda(x: str, config: RunnableConfig) -> str:
        """Sonda."""
        vistas.append((f"antes-{x}", idempotency.write_key(config)))
        interrupt({"x": x})
        vistas.append((f"despues-{x}", idempotency.write_key(config)))
        return "ok"

    model = FakeToolCallingModel(
        scripted_tool_calls=[
            {"name": "sonda", "args": {"x": "a"}, "id": "call_a"},
            {"name": "sonda", "args": {"x": "b"}, "id": "call_b"},
        ]
    )
    graph = create_react_agent(model, [sonda], checkpointer=MemorySaver(), version="v2")
    config = {"configurable": {"thread_id": "claves-1"}}

    paused = graph.invoke({"messages": [("user", "dale")]}, config)
    graph.invoke(Command(resume={i.id: {"ok": True} for i in paused["__interrupt__"]}), config)

    claves = dict(vistas)
    assert claves["antes-a"] is not None
    assert claves["antes-b"] is not None
    # Estable a traves de la pausa.
    assert claves["antes-a"] == claves["despues-a"]
    assert claves["antes-b"] == claves["despues-b"]
    # Distinta entre hermanas del mismo mensaje.
    assert claves["antes-a"] != claves["antes-b"]


def test_the_write_key_is_none_without_a_graph_behind_it():
    """Una invocacion directa no se puede reanudar: no hay nada que
    desduplicar y la guardia se desactiva sola en vez de reventar."""
    assert idempotency.write_key({"configurable": {"thread_id": "x"}}) is None
    assert idempotency.write_key({}) is None
    assert idempotency.write_key(None) is None


def test_the_write_key_is_scoped_to_the_thread():
    a = idempotency.write_key({"configurable": {"thread_id": "t1", "checkpoint_ns": "tools:abc"}})
    b = idempotency.write_key({"configurable": {"thread_id": "t2", "checkpoint_ns": "tools:abc"}})

    assert a != b


# ---------------------------------------------------------------------
# La marca: la segunda pasada no escribe
# ---------------------------------------------------------------------


def _movimiento(monto="50.00"):
    return {"tipo": "aporte_socio", "monto": monto, "fecha": "2026-09-24"}


def test_a_replayed_cash_movement_task_does_not_write_twice(db_session, seeded_user, monkeypatch):
    """`registrar_movimiento_caja` "duplica SIEMPRE" sin esta guardia: no
    tiene nada con que distinguir una repeticion de una solicitud nueva."""
    monkeypatch.setattr("app.agent.tools.write.interrupt", lambda _p: {"accion": "aprobar"})
    config = _config(seeded_user, uuid.uuid4())

    primero = registrar_movimiento_caja.invoke(_movimiento(), config=config)
    segundo = registrar_movimiento_caja.invoke(_movimiento(), config=config)

    assert primero["estado"] == "registrado"
    assert segundo["estado"] == "ya_registrado"
    assert segundo["movimiento_id"] == primero["movimiento_id"]
    assert db_session.query(CashMovement).count() == 1


def test_a_different_task_in_the_same_thread_still_writes(db_session, seeded_user, monkeypatch):
    """La guardia no puede comerse una solicitud nueva y legitima."""
    monkeypatch.setattr("app.agent.tools.write.interrupt", lambda _p: {"accion": "aprobar"})

    registrar_movimiento_caja.invoke(_movimiento(), config=_config(seeded_user, uuid.uuid4()))
    registrar_movimiento_caja.invoke(_movimiento(), config=_config(seeded_user, uuid.uuid4()))

    assert db_session.query(CashMovement).count() == 2


def test_a_replayed_sale_task_does_not_write_twice(
    db_session, seeded_customer, seeded_product_with_lot, seeded_user, monkeypatch
):
    """La venta "escapaba solo por accidente": con un lote de sobra al mismo
    costo, la huella recalculada de la segunda pasada coincidia con la
    primera y la guardia la dejaba pasar. El lote sembrado tiene 6 unidades
    y la venta se lleva 1: sin marca, la segunda pasada escribiria."""
    monkeypatch.setattr("app.agent.tools.write.interrupt", _approve)
    config = _config(seeded_user, uuid.uuid4())
    payload = {
        "cliente_id": str(seeded_customer.id),
        "items": [{"producto_id": str(seeded_product_with_lot.id), "cantidad": "1"}],
        "fecha": "2026-09-24",
    }

    primero = registrar_venta.invoke(dict(payload), config=config)
    segundo = registrar_venta.invoke(dict(payload), config=config)

    assert primero["estado"] == "registrado"
    assert segundo["estado"] == "ya_registrado"
    assert segundo["venta_id"] == primero["venta_id"]
    assert db_session.query(Sale).count() == 1


def test_the_replay_answer_says_the_write_already_happened_not_that_stock_changed(
    db_session, seeded_customer, seeded_product_with_lot, seeded_user, monkeypatch
):
    """El chequeo va ANTES del interrupt a proposito. Si fuera despues, el
    recalculo de la segunda pasada veria el lote que la PRIMERA consumio,
    la huella no coincidiria y la herramienta contestaria "recalculado" --
    "el inventario cambio": cierto y engañoso, porque quien lo cambio fue
    esta misma tarea al escribir."""
    monkeypatch.setattr("app.agent.tools.write.interrupt", _approve)
    config = _config(seeded_user, uuid.uuid4())
    payload = {
        "cliente_id": str(seeded_customer.id),
        "items": [{"producto_id": str(seeded_product_with_lot.id), "cantidad": "3"}],
        "fecha": "2026-09-24",
    }

    registrar_venta.invoke(dict(payload), config=config)
    segundo = registrar_venta.invoke(dict(payload), config=config)

    assert segundo["estado"] == "ya_registrado"


def test_a_failed_write_leaves_no_mark_behind(
    db_session, seeded_customer, seeded_product_with_lot, seeded_user, monkeypatch
):
    """La marca y la escritura comparten transaccion: o quedan las dos o no
    queda ninguna. Si la marca se comiteara aparte, un intento que reviente
    dejaria la tarea marcada como "ya escribio" y la venta que se le pidio
    no se escribiria nunca."""
    monkeypatch.setattr("app.agent.tools.write.interrupt", _approve)

    from app.services.sales import SaleService

    real_create = SaleService.create_enriched
    fallos = {"restantes": 1}

    def explode_once(self, data, user_id):
        if fallos["restantes"]:
            fallos["restantes"] -= 1
            raise RuntimeError("la base se cayo al escribir")
        return real_create(self, data, user_id=user_id)

    monkeypatch.setattr(SaleService, "create_enriched", explode_once)

    clave_config = _config(seeded_user, uuid.uuid4())
    payload = {
        "cliente_id": str(seeded_customer.id),
        "items": [{"producto_id": str(seeded_product_with_lot.id), "cantidad": "1"}],
        "fecha": "2026-09-24",
    }

    with pytest.raises(RuntimeError):
        registrar_venta.invoke(dict(payload), config=clave_config)

    clave = idempotency.write_key(clave_config)
    marcas = db_session.execute(
        text(f"SELECT count(*) FROM {idempotency.QUALIFIED} WHERE write_key = :k"),
        {"k": clave},
    ).scalar()
    assert marcas == 0

    # Y por lo tanto el reintento de la misma tarea SI escribe.
    reintento = registrar_venta.invoke(dict(payload), config=clave_config)
    assert reintento["estado"] == "registrado"
    assert db_session.query(Sale).count() == 1


def test_direct_invocations_without_a_task_id_are_never_deduplicated(
    db_session, seeded_user, monkeypatch
):
    """Sin grafo detras no hay reanudacion, y dos llamadas son dos hechos
    distintos -- la guardia no puede inventarse una equivalencia."""
    monkeypatch.setattr("app.agent.tools.write.interrupt", lambda _p: {"accion": "aprobar"})
    config = {"configurable": {AUTH_USER_ID_KEY: str(seeded_user.id)}}

    registrar_movimiento_caja.invoke(_movimiento(), config=config)
    registrar_movimiento_caja.invoke(_movimiento(), config=config)

    assert db_session.query(CashMovement).count() == 2


def test_the_mark_lands_in_the_agent_schema_not_the_business_one(db_session, seeded_user, monkeypatch):
    """Una tabla sin modelo en `app/models` dentro del schema del negocio la
    leeria el autogenerate de Alembic como deriva y emitiria un `drop_table`
    en cada migracion. Por eso vive donde ya viven las tablas del
    checkpointer."""
    monkeypatch.setattr("app.agent.tools.write.interrupt", lambda _p: {"accion": "aprobar"})
    config = _config(seeded_user, uuid.uuid4())
    registrar_movimiento_caja.invoke(_movimiento(), config=config)

    schema = db_session.execute(
        text(
            "SELECT table_schema FROM information_schema.tables "
            "WHERE table_name = :t AND table_schema = :s"
        ),
        {"t": idempotency.TABLE, "s": idempotency.AGENT_SCHEMA},
    ).scalar()
    assert schema == idempotency.AGENT_SCHEMA

    current = db_session.execute(text("SELECT current_schema()")).scalar()
    assert current != idempotency.AGENT_SCHEMA
