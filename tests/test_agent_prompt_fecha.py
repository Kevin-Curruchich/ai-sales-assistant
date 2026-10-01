"""El agente tiene que saber que dia es hoy, y saberlo en cada turno.

## Por que existe este archivo

`previsualizar_venta`, `registrar_venta`, `registrar_compra` y
`registrar_movimiento_caja` reciben `fecha` como parametro OBLIGATORIO, y nada
se la decia al modelo: ni el prompt ni el grafo mencionaban la fecha actual. El
modelo la inventaba desde su entrenamiento, o sea una fecha pasada.

El dano no era cosmetico. El FIFO filtra los lotes con
`purchase.date <= fecha_de_la_venta` (`PurchaseRepository.get_fifo_available_lots`),
asi que una fecha inventada anterior a la compra EXCLUYE el lote. Medido contra
produccion el 2026-10-01, con todo el inventario abierto el 2026-09-30 por el
corte: con la fecha real el FIFO ve 1 lote y 6 unidades; con cualquier fecha
anterior al 30/09 ve 0 y 0. El agente respondia "el producto tiene stock pero
esta sin lotes" y no podia registrar ninguna venta, mientras el panel -- que
manda la fecha real del navegador -- funcionaba.

## El test que importa

`test_la_fecha_se_resuelve_en_cada_turno...`: el grafo se construye UNA SOLA VEZ,
en el `lifespan` de `app/main.py`, y vive mientras viva el proceso. Una fecha
calculada al construirlo se congela el dia del despliegue y el bug vuelve a los
pocos dias, mas dificil de ver porque "funcionaba cuando lo probamos".
"""

from datetime import date

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from app.agent.prompt import SYSTEM_PROMPT
from app.agent.prompt_runtime import prompt_con_fecha


def _texto_del_sistema(mensajes) -> str:
    """El contenido del SystemMessage que encabeza lo que recibe el modelo."""
    primero = mensajes[0]
    return primero.content if isinstance(primero.content, str) else str(primero.content)


def test_el_prompt_le_dice_al_modelo_que_dia_es_hoy(monkeypatch):
    monkeypatch.setattr("app.agent.prompt_runtime._hoy", lambda: date(2026, 10, 1))

    mensajes = prompt_con_fecha({"messages": [HumanMessage("vendi un carton")]})

    assert "2026-10-01" in _texto_del_sistema(mensajes)


def test_la_fecha_se_resuelve_en_cada_turno_y_no_al_construir_el_grafo(monkeypatch):
    """La regresion que este archivo existe para impedir.

    El grafo se construye una vez por proceso. Si la fecha se calculara ahi,
    quedaria congelada el dia del despliegue: al dia siguiente el agente seguiria
    creyendo que es ayer, y una semana despues sus ventas volverian a caer fuera
    del rango de los lotes. Dos llamadas con relojes distintos tienen que dar
    fechas distintas.
    """
    monkeypatch.setattr("app.agent.prompt_runtime._hoy", lambda: date(2026, 10, 1))
    primero = _texto_del_sistema(prompt_con_fecha({"messages": [HumanMessage("hola")]}))

    monkeypatch.setattr("app.agent.prompt_runtime._hoy", lambda: date(2026, 10, 8))
    segundo = _texto_del_sistema(prompt_con_fecha({"messages": [HumanMessage("hola")]}))

    assert "2026-10-01" in primero
    assert "2026-10-08" in segundo
    assert "2026-10-08" not in primero


def test_la_fecha_sale_de_la_zona_del_negocio_y_no_del_reloj_del_servidor():
    """Railway corre en UTC y Guatemala esta seis horas atras, asi que entre las
    18:00 y la medianoche local el servidor ya paso al dia siguiente. Un
    `date.today()` fecharia manana una venta de las 19:00.

    El test afirma la propiedad directamente -- que al reloj se le pide la hora
    EN LA ZONA DEL NEGOCIO -- en vez de simular un instante y comparar: asi falla
    ante cualquier forma de leer el reloj sin zona, no solo ante la que se me
    ocurrio simular.
    """
    from datetime import datetime as datetime_real
    from unittest.mock import patch

    from app.agent.prompt_runtime import _hoy
    from app.core.datetime_utils import business_tz

    with patch("app.agent.prompt_runtime.datetime") as reloj:
        reloj.now.return_value = datetime_real(2026, 10, 1, 20, 0)
        _hoy()

    reloj.now.assert_called_once_with(business_tz())


def test_el_prompt_del_sistema_sigue_entero():
    """La fecha se AGREGA, no reemplaza: las reglas de negocio (FIFO, margenes,
    vocabulario de caja) siguen siendo lo que hace util al agente."""
    mensajes = prompt_con_fecha({"messages": [HumanMessage("hola")]})
    texto = _texto_del_sistema(mensajes)

    assert SYSTEM_PROMPT in texto
    assert len(texto) > len(SYSTEM_PROMPT)


def test_la_conversacion_viaja_despues_del_sistema_y_sin_tocar():
    """El callable reemplaza la lista entera que recibe el modelo, asi que si se
    olvidara de reenviar los mensajes, el agente perderia la conversacion
    completa en cada turno."""
    historia = [
        HumanMessage("vendi un carton a Aurita"),
        AIMessage("Son Q38. Confirmas?"),
        HumanMessage("si"),
    ]

    mensajes = prompt_con_fecha({"messages": historia})

    assert mensajes[1:] == historia


@pytest.mark.parametrize("estado", [{"messages": []}, {}])
def test_un_estado_sin_mensajes_no_revienta(estado):
    """El primer turno de un hilo recien creado, y el caso defensivo de un estado
    sin la clave: un `KeyError` aca seria un evento `error` generico y el panel
    no tendria forma de saber que paso."""
    mensajes = prompt_con_fecha(estado)

    assert len(mensajes) == 1
    assert "2026" in _texto_del_sistema(mensajes)


def test_el_grafo_real_le_entrega_la_fecha_al_modelo(monkeypatch):
    """De punta a punta, sin doble del prompt: se construye el grafo como lo
    construye produccion y se mira lo que el modelo REALMENTE recibe.

    Los tests de arriba prueban la funcion; este prueba que este cableada. Sin
    el, `build_graph` podria seguir pasando el string fijo y todo lo demas
    seguiria verde.
    """
    from langgraph.checkpoint.memory import MemorySaver

    from app.agent.graph import build_graph
    from tests.test_agent_graph import FakeToolCallingModel

    monkeypatch.setattr("app.agent.prompt_runtime._hoy", lambda: date(2026, 10, 1))

    recibidos = {}

    class ModeloEspia(FakeToolCallingModel):
        def _generate(self, messages, *args, **kwargs):
            recibidos["messages"] = messages
            return super()._generate(messages, *args, **kwargs)

    grafo = build_graph(ModeloEspia(scripted_tool_calls=[]), MemorySaver())
    grafo.invoke(
        {"messages": [HumanMessage("vendi un carton")]},
        {"configurable": {"thread_id": "t-fecha"}},
    )

    texto = " ".join(
        m.content if isinstance(m.content, str) else str(m.content)
        for m in recibidos["messages"]
    )
    assert "2026-10-01" in texto
