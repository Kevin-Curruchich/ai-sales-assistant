"""`agent_session` abre y cierra una Session; `user_id_from_config` es el
unico canal por el que las herramientas de escritura (Task 8) saben quien
firma -- nunca el modelo."""

import uuid

import pytest

from app.agent.session import AgentAuthError, agent_session, user_id_from_config


def test_agent_session_yields_a_session_and_closes_it_after():
    with agent_session() as db:
        assert db.execute is not None
        assert db.is_active


def test_user_id_from_config_reads_the_injected_user_id():
    known_id = uuid.uuid4()
    config = {"configurable": {"user_id": str(known_id)}}

    assert user_id_from_config(config) == known_id


def test_user_id_from_config_accepts_a_uuid_object_directly():
    known_id = uuid.uuid4()
    config = {"configurable": {"user_id": known_id}}

    assert user_id_from_config(config) == known_id


def test_user_id_from_config_raises_when_configurable_is_missing():
    with pytest.raises(AgentAuthError):
        user_id_from_config({})


def test_user_id_from_config_raises_when_user_id_is_missing():
    with pytest.raises(AgentAuthError):
        user_id_from_config({"configurable": {}})


def test_user_id_from_config_raises_when_user_id_is_none():
    with pytest.raises(AgentAuthError):
        user_id_from_config({"configurable": {"user_id": None}})


def test_user_id_from_config_raises_when_user_id_is_not_a_valid_uuid():
    with pytest.raises(AgentAuthError):
        user_id_from_config({"configurable": {"user_id": "no-es-un-uuid"}})
