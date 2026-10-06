"""Toda ruta de /api/v1 exige token, salvo las que estan en PUBLIC_ROUTES.

La lista de rutas sale de la app misma, no de una lista escrita a mano: una
ruta nueva queda cubierta sin tocar este archivo, y si se agrega sin
autenticacion (o un router pierde su `dependencies=[Depends(get_current_user)]`)
este test falla. Hacer publica una ruta es una decision explicita: agregarla
a PUBLIC_ROUTES.
"""

import re
import uuid

import pytest
from fastapi.routing import APIRoute

import app.main as main

PUBLIC_ROUTES = {
    ("POST", "/api/v1/auth/signup"),
}


def _api_routes():
    for route in main.app.routes:
        if not isinstance(route, APIRoute) or not route.path.startswith("/api/v1"):
            continue
        for method in sorted(route.methods - {"HEAD", "OPTIONS"}):
            yield method, route.path


PROTECTED_ROUTES = [r for r in _api_routes() if r not in PUBLIC_ROUTES]


def test_the_route_list_is_not_empty():
    # Si el recorrido de rutas se rompiera, el test parametrizado de abajo
    # pasaria sin probar nada.
    assert len(PROTECTED_ROUTES) > 30


def test_public_routes_still_exist():
    assert PUBLIC_ROUTES <= set(_api_routes())


@pytest.mark.parametrize("method, path", PROTECTED_ROUTES, ids=lambda v: v)
def test_route_rejects_requests_without_a_token(client, method, path):
    url = re.sub(r"\{[^}]+\}", str(uuid.uuid4()), path)

    resp = client.request(method, url, json={})

    assert resp.status_code in (401, 403), f"{method} {path} -> {resp.status_code}"
