"""`agent_session` abre y cierra una Session; `user_id_from_config` es el
unico canal por el que las herramientas de escritura (Task 8) saben quien
firma -- nunca el modelo, y nunca el llamador."""

import uuid

import pytest
from sqlalchemy.orm import Session

from app.agent.session import (
    AgentAuthError,
    agent_session,
    user_id_from_config,
)

UN_UUID = uuid.uuid4()


def test_agent_session_yields_a_session_and_closes_it_after():
    """La asercion que importa es la de DESPUES del `with`: sin ella el test
    no podia atrapar un `finally` roto, que es justo lo que este
    contextmanager existe para garantizar (el proceso del agente no tiene
    `Depends` que cierre la sesion por el)."""
    with agent_session() as db:
        assert isinstance(db, Session)
        assert db.is_active
        captured = db

    # `Session.close()` suelta la conexion al pool y deja la sesion sin
    # transaccion abierta. `get_transaction()` devuelve None recien despues
    # de cerrar; mientras la sesion vive (aunque no se haya usado) SQLAlchemy
    # mantiene el `SessionTransaction` autobegin disponible.
    assert captured.get_transaction() is None


def test_agent_session_closes_the_session_even_when_the_body_raises():
    captured = {}

    with pytest.raises(RuntimeError):
        with agent_session() as db:
            captured["db"] = db
            raise RuntimeError("la herramienta reviento")

    assert captured["db"].get_transaction() is None


def test_reads_the_user_id_the_endpoint_injected():
    config = {"configurable": {"user_id": str(UN_UUID)}}
    assert user_id_from_config(config) == UN_UUID


def test_accepts_a_uuid_object_directly():
    known_id = uuid.uuid4()
    config = {"configurable": {"user_id": known_id}}

    assert user_id_from_config(config) == known_id


@pytest.mark.parametrize(
    "config",
    [
        {},
        {"configurable": {}},
        {"configurable": {"user_id": None}},
        {"configurable": {"user_id": ""}},
        {"configurable": {"user_id": "no-es-un-uuid"}},
    ],
)
def test_refuses_anything_that_is_not_a_user_id(config):
    with pytest.raises(AgentAuthError):
        user_id_from_config(config)
