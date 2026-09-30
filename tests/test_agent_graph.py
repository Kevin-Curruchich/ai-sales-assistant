"""El grafo suspende de verdad, el estado sobrevive, y una escritura que ya
comiteo no se repite cuando una hermana del mismo mensaje del modelo sigue
pausada en un `interrupt()`.

El ultimo punto es la condicion de aceptacion bloqueante de Task 9 (no esta
en el brief de la task): `ToolNode` corre todas las tool_calls de un mismo
mensaje como UNA sola tarea de Pregel -- confirmado contra langgraph 1.0.3
con un `ToolNode` cableado a mano (ver el docstring de
`app/agent/tools/write.py`). `build_graph` (Task 9) lo cierra usando
`create_react_agent(..., version="v2")`, que reparte cada tool_call del
mismo mensaje con `Send()` -- cada una su propia tarea, con su propio
registro de "pending writes" en el checkpoint. El ultimo test de este
archivo lo prueba de punta a punta: un checkpointer real, DOS herramientas
de escritura reales pedidas en el MISMO mensaje del modelo, una de ellas
interrumpida, y la asercion de que el cuerpo de la que ya comiteo corre
exactamente una vez a lo largo de dos rondas de reanudacion.
"""

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

from app.agent.graph import build_graph, checkpointer_schema
from app.models.cash_movement import CashMovement
from app.models.sale import Sale


class FakeToolCallingModel(BaseChatModel):
    """Modelo falso, sin NLU: en el primer turno emite, en UN solo
    `AIMessage`, exactamente las tool_calls con las que se lo construyo --
    tal como haria un modelo real que pide mas de una herramienta a la vez.
    Despues del primer lote de `ToolMessage`, cierra con texto plano, para
    que el grafo pueda terminar la corrida sin pedir mas herramientas."""

    scripted_tool_calls: list[dict] = []
    n: int = 0

    def bind_tools(self, tools, **kwargs):
        return self

    @property
    def _llm_type(self) -> str:
        return "fake-tool-calling"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        self.n += 1
        if self.n == 1 and self.scripted_tool_calls:
            msg = AIMessage(content="", tool_calls=self.scripted_tool_calls)
        else:
            msg = AIMessage(content="listo")
        return ChatResult(generations=[ChatGeneration(message=msg)])


def _sale_tool_call(customer, product, *, call_id="call_1", cantidad="1"):
    return {
        "name": "registrar_venta",
        "args": {
            "cliente_id": str(customer.id),
            "items": [{"producto_id": str(product.id), "cantidad": cantidad}],
            "fecha": "2026-09-24",
        },
        "id": call_id,
    }


def test_the_graph_interrupts_before_writing(db_session, seeded_customer, seeded_product_with_lot, seeded_user):
    """Con un modelo falso que pide registrar una venta, el grafo se detiene."""
    model = FakeToolCallingModel(scripted_tool_calls=[_sale_tool_call(seeded_customer, seeded_product_with_lot)])
    graph = build_graph(model=model, checkpointer=MemorySaver())
    config = {"configurable": {"thread_id": "t1", "user_id": str(seeded_user.id)}}

    result = graph.invoke({"messages": [("user", "vendi un carton a Aurita")]}, config)

    assert "__interrupt__" in result
    assert db_session.query(Sale).count() == 0


def test_resuming_after_the_interrupt_continues_from_inside_the_tool(
    db_session, seeded_customer, seeded_product_with_lot, seeded_user
):
    model = FakeToolCallingModel(scripted_tool_calls=[_sale_tool_call(seeded_customer, seeded_product_with_lot)])
    graph = build_graph(model=model, checkpointer=MemorySaver())
    config = {"configurable": {"thread_id": "t2", "user_id": str(seeded_user.id)}}
    graph.invoke({"messages": [("user", "vendi un carton a Aurita")]}, config)

    final = graph.invoke(Command(resume={"accion": "cancelar"}), config)

    assert "__interrupt__" not in final
    assert db_session.query(Sale).count() == 0


def test_the_checkpointer_never_targets_the_business_schema():
    """Las tablas de LangGraph no pueden vivir con las del negocio: Alembic
    las leeria como deriva y emitiria `drop_table` para cada una en cada
    migracion.

    La version anterior de este test afirmaba que `checkpointer_schema()`
    devuelve la constante que su cuerpo devuelve -- inerte: cambiar la
    constante cambiaba el test con ella y el invariante real seguia sin
    vigilancia. El invariante real es la SEPARACION respecto del schema que
    Alembic recorre, y eso es lo que se afirma aca."""
    from app.core.config import settings

    assert checkpointer_schema() != settings.POSTGRES_SCHEMA
    assert checkpointer_schema() not in ("public", "")


def test_a_completed_write_tool_does_not_replay_when_a_sibling_write_tool_is_still_interrupted(
    db_session, seeded_customer, seeded_product_with_lot, seeded_purchase_draft, seeded_user
):
    """Condicion de aceptacion bloqueante de Task 9 (no esta en el brief).

    El modelo falso pide, en el MISMO mensaje, `registrar_movimiento_caja`
    (un aporte del socio a una compra en borrador) y `registrar_venta` -- las
    dos herramientas de escritura REALES, no un doble de prueba. La primera
    ronda de reanudacion aprueba la caja (que termina y comitea ahi mismo) y
    corrige la venta (fuerza que su tarea vuelva a pausarse ELLA SOLA, en la
    misma corrida, con una tarea hermana ya resuelta).

    Si `ToolNode` corriera las dos tool_calls como una sola tarea -- el
    limite documentado en `app/agent/tools/write.py` -- reanudar la venta en
    la segunda ronda volveria a correr TODO el paso desde el principio,
    incluido `registrar_movimiento_caja`, que "duplica siempre: no tiene
    ninguna guardia que pueda distinguir una repeticion de una solicitud
    nueva" (docstring de write.py). La asercion central es que eso no pasa:
    el `CashMovement` que la caja escribio en la ronda 1 sigue siendo uno
    solo despues de la ronda 2.
    """
    config = {"configurable": {"thread_id": "batch-1", "user_id": str(seeded_user.id)}}

    model = FakeToolCallingModel(
        scripted_tool_calls=[
            {
                "name": "registrar_movimiento_caja",
                "args": {
                    "tipo": "aporte_socio",
                    "monto": "60.00",
                    "fecha": "2026-09-20",
                    "compra_id": str(seeded_purchase_draft.id),
                },
                "id": "call_caja",
            },
            _sale_tool_call(seeded_customer, seeded_product_with_lot, call_id="call_venta", cantidad="2"),
        ]
    )
    graph = build_graph(model=model, checkpointer=MemorySaver())

    first = graph.invoke({"messages": [("user", "aporte del socio a la compra, y vendi 2")]}, config)
    interrupts = first["__interrupt__"]
    assert len(interrupts) == 2
    by_tipo = {i.value["tipo"]: i for i in interrupts}
    caja_interrupt = by_tipo["confirmar_movimiento_caja"]
    venta_interrupt = by_tipo["confirmar_venta"]

    resume_round_1 = {
        caja_interrupt.id: {"accion": "aprobar"},
        venta_interrupt.id: {
            "accion": "corregir",
            "valores": {"items": [{"producto_id": str(seeded_product_with_lot.id), "cantidad": "1"}]},
        },
    }
    second = graph.invoke(Command(resume=resume_round_1), config)

    # La caja ya escribio -- exactamente una vez -- ANTES de que la venta
    # vuelva a pausarse.
    assert db_session.query(CashMovement).filter_by(purchase_id=seeded_purchase_draft.id).count() == 1

    remaining_interrupts = second["__interrupt__"]
    assert len(remaining_interrupts) == 1
    assert remaining_interrupts[0].value["tipo"] == "confirmar_venta"
    nueva_huella = remaining_interrupts[0].value["huella"]

    resume_round_2 = {remaining_interrupts[0].id: {"accion": "aprobar", "huella": nueva_huella}}
    final = graph.invoke(Command(resume=resume_round_2), config)

    assert "__interrupt__" not in final
    assert db_session.query(Sale).count() == 1
    # La asercion central: la caja sigue en UNO despues de que la venta,
    # hermana suya en el mismo mensaje del modelo, termino de reanudarse.
    assert db_session.query(CashMovement).filter_by(purchase_id=seeded_purchase_draft.id).count() == 1


