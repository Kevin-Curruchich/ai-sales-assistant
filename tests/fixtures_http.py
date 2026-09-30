"""Harness de tests HTTP autenticados: un `client` (TestClient sobre
app.main.app) con `get_db` y `get_current_user` apuntados a la sesion de
prueba (mismo schema desechable que siembra `db_session`), mas `_auth` y
`_create_thread_as`.

Este repo no tenia ni un test de endpoint autenticado -- `test_startup.py`
solo golpea `/health`, que es publico. La Task 6 consume estos nombres; no
son un detalle de implementacion de esta task.
"""

import uuid

import pytest
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from fastapi.testclient import TestClient

import app.main as main
from app.api.dependencies import bearer_scheme, get_current_user, get_db
from app.models import User


@pytest.fixture
def client(db_session):
    """TestClient sobre app.main.app.

    `get_db` se overridea para devolver la misma `db_session` que siembra
    `seeded_user`/`second_user` (schema desechable, puerto 55432) -- la
    `SessionLocal` de la app apunta por defecto a la base de desarrollo
    (puerto 55433) y nunca veria esos datos.

    `get_current_user` se overridea para resolver el usuario directo contra
    esa misma sesion, usando el id de usuario que viaja como si fuera el
    token (ver `_auth`), sin pasar por Firebase.
    """

    def _override_get_db():
        yield db_session

    def _override_get_current_user(
        credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
    ) -> User:
        try:
            user_id = uuid.UUID(credentials.credentials)
        except ValueError:
            raise HTTPException(status_code=401, detail="token de prueba invalido")
        user = db_session.get(User, user_id)
        if user is None:
            raise HTTPException(status_code=401, detail="usuario de prueba desconocido")
        return user

    main.app.dependency_overrides[get_db] = _override_get_db
    main.app.dependency_overrides[get_current_user] = _override_get_current_user
    try:
        with TestClient(main.app) as test_client:
            yield test_client
    finally:
        main.app.dependency_overrides.clear()


def _auth(user: User) -> dict:
    """Cabeceras de autenticacion para un usuario de prueba: el id del
    usuario viaja como si fuera el token; el override de `get_current_user`
    en `client` lo resuelve directo contra la sesion de prueba."""
    return {"Authorization": f"Bearer {user.id}"}


def _create_thread_as(client: TestClient, user: User, title: str) -> dict:
    resp = client.post(
        "/api/v1/agent/threads",
        json={"title": title},
        headers=_auth(user),
    )
    assert resp.status_code == 201, resp.text
    return resp.json()
