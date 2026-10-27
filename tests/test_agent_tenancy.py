"""El agente corre cada herramienta dentro del comercio de quien llama, y el
prompt usa la zona horaria de ese comercio."""

import uuid
from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from langchain_core.messages import HumanMessage

import app.main as main
from app.agent.auth import AgentAuthError
from app.agent.prompt_runtime import prompt_con_fecha
from app.agent.session import agent_session
from app.agent.tools.write import registrar_movimiento_caja
from app.api.dependencies import bearer_scheme, get_current_user, get_db
from app.core.database import bind_business
from app.models.cash_movement import CashMovement
from app.models.user import User
from tests.fixtures_domain import agent_config, business_configurable
from tests.fixtures_http import _auth, _create_thread_as


@pytest.mark.parametrize(
    "configurable",
    [
        {"user_id": str(uuid.uuid4())},
        {"business_id": str(uuid.uuid4())},
        {"business_tz": "America/Guatemala"},
    ],
)
def test_agent_session_requires_business(configurable):
    with pytest.raises(AgentAuthError):
        with agent_session({"configurable": configurable}):
            pass


@pytest.fixture
def tenant_client(client, db_session, tenant_session):
    """`client` con `get_db` sobre `tenant_session` (sujeta a RLS): sin esto,
    `db_session` es superusuario y el 404 no probaria nada del aislamiento."""

    def _get_db():
        yield tenant_session

    def _get_current_user(
        credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
    ) -> User:
        # El usuario se resuelve como superusuario (como lo hace la app antes
        # de saber el comercio) y se liga la sesion RLS a su comercio.
        user = db_session.get(User, uuid.UUID(credentials.credentials))
        if user is None:
            raise HTTPException(status_code=401)
        bind_business(tenant_session, user.business_id, user.business.timezone)
        return user

    main.app.dependency_overrides[get_db] = _get_db
    main.app.dependency_overrides[get_current_user] = _get_current_user
    return client


def test_thread_of_other_business_is_404(
    tenant_client, tenant_session, seeded_user, user_of_other_business
):
    client = tenant_client
    t = _create_thread_as(client, user_of_other_business, "ajeno")
    # Sin cache de identidad: que el 404 salga de la base (RLS), no de un
    # objeto que la sesion ya tenia en memoria.
    tenant_session.expunge_all()
    for method, path in [
        ("get", f"/api/v1/agent/threads/{t['id']}"),
        ("get", f"/api/v1/agent/threads/{t['id']}/state"),
    ]:
        assert (
            getattr(client, method)(path, headers=_auth(seeded_user)).status_code == 404
        )
    r = client.post(
        "/api/v1/agent/stream",
        json={"thread_id": t["id"], "mensaje": "hola"},
        headers=_auth(seeded_user),
    )
    assert r.status_code == 404


def test_stream_puts_the_callers_business_in_configurable(
    client, seeded_user, monkeypatch
):
    capturado = {}

    async def _pausas(_graph, config):
        capturado.update(config["configurable"])
        return set()

    monkeypatch.setattr("app.api.v1.endpoints.agent._pausas_pendientes", _pausas)
    t = _create_thread_as(client, seeded_user, "propio")
    client.post(
        "/api/v1/agent/stream",
        json={"thread_id": t["id"], "mensaje": "hola"},
        headers=_auth(seeded_user),
    )

    assert capturado["business_id"] == str(seeded_user.business_id)
    assert capturado["business_tz"] == seeded_user.business.timezone


def test_write_tool_creates_rows_in_callers_business(
    db_session, user_of_other_business, other_business, monkeypatch
):
    monkeypatch.setattr(
        "app.agent.tools.write.interrupt", lambda _p: {"accion": "aprobar"}
    )

    result = registrar_movimiento_caja.invoke(
        {"tipo": "aporte_socio", "monto": "10.00", "fecha": "2026-10-01"},
        config=agent_config(user_of_other_business),
    )

    movement = db_session.get(CashMovement, uuid.UUID(result["movimiento_id"]))
    assert movement.business_id == other_business.id


def test_prompt_uses_business_timezone():
    # 03:00 UTC del 2 de octubre: en Guatemala todavia es 1 de octubre.
    instante = datetime(2026, 10, 2, 3, 0, tzinfo=timezone.utc)

    class _Reloj(datetime):
        @classmethod
        def now(cls, tz=None):
            return instante.astimezone(tz)

    def _sistema(tz):
        config = {"configurable": business_configurable(uuid.uuid4(), tz)}
        with patch("app.agent.prompt_runtime.datetime", _Reloj):
            mensajes = prompt_con_fecha({"messages": [HumanMessage("hola")]}, config)
        return mensajes[0].content

    assert "Hoy es 2026-10-01" in _sistema("America/Guatemala")
    assert "Hoy es 2026-10-02" in _sistema("Asia/Tokyo")
