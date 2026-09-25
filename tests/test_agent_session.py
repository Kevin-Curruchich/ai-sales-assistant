"""`agent_session` abre y cierra una Session; `user_id_from_config` es el
unico canal por el que las herramientas de escritura (Task 8) saben quien
firma -- nunca el modelo, y nunca el llamador."""

import uuid

import pytest
from sqlalchemy.orm import Session

from app.agent.session import (
    AUTH_USER_ID_KEY,
    AUTH_USER_KEY,
    AgentAuthError,
    agent_session,
    user_id_from_config,
)


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


def test_user_id_from_config_reads_the_identity_the_server_injected():
    known_id = uuid.uuid4()
    config = {"configurable": {AUTH_USER_ID_KEY: str(known_id)}}

    assert user_id_from_config(config) == known_id


def test_user_id_from_config_accepts_a_uuid_object_directly():
    known_id = uuid.uuid4()
    config = {"configurable": {AUTH_USER_ID_KEY: known_id}}

    assert user_id_from_config(config) == known_id


def test_user_id_from_config_falls_back_to_the_user_objects_identity():
    """El servidor pone las dos claves. Si por lo que sea llega solo el
    objeto normalizado (`ProxyUser`/`SimpleUser`), su `identity` sirve
    igual."""
    known_id = uuid.uuid4()

    class FakeUser:
        identity = str(known_id)

    config = {"configurable": {AUTH_USER_KEY: FakeUser()}}

    assert user_id_from_config(config) == known_id


def test_user_id_from_config_refuses_a_caller_asserted_user_id():
    """`user_id` a secas NO alcanza, y esa es la correccion central.

    No esta en `langgraph_api.validation.RESERVED_CONFIGURABLE_KEYS`, asi que
    el servidor deja que cualquiera lo mande en el `config` de la corrida:
    aceptarlo aca era dejar que quien alcanzara el puerto escribiera como
    quien quisiera. Solo vale la identidad que el servidor derivo del token
    de Firebase."""
    with pytest.raises(AgentAuthError):
        user_id_from_config({"configurable": {"user_id": str(uuid.uuid4())}})


def test_user_id_from_config_raises_when_configurable_is_missing():
    with pytest.raises(AgentAuthError):
        user_id_from_config({})


def test_user_id_from_config_raises_when_the_identity_is_missing():
    with pytest.raises(AgentAuthError):
        user_id_from_config({"configurable": {}})


def test_user_id_from_config_raises_when_the_identity_is_none():
    with pytest.raises(AgentAuthError):
        user_id_from_config({"configurable": {AUTH_USER_ID_KEY: None}})


def test_user_id_from_config_raises_when_the_identity_is_not_a_valid_uuid():
    with pytest.raises(AgentAuthError):
        user_id_from_config({"configurable": {AUTH_USER_ID_KEY: "no-es-un-uuid"}})
