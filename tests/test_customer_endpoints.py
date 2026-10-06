"""Endpoints de clientes: fijan el contrato HTTP (codigos y forma de la
respuesta) para que el cableado de dependencias del router se pueda cambiar
sin que cambie lo que ve el panel. Que exijan token lo cubre
tests/test_api_auth_required.py para toda la API."""

import uuid

from tests.fixtures_http import _auth


def test_create_returns_the_new_customer(client, seeded_user):
    resp = client.post(
        "/api/v1/customers",
        json={"name": "Ana López", "email": "ana@example.com", "phone": "5555-0000"},
        headers=_auth(seeded_user),
    )

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["name"] == "Ana López"
    assert body["email"] == "ana@example.com"
    uuid.UUID(body["id"])
    assert body["created_at"] and body["updated_at"]


def test_create_rejects_an_invalid_email(client, seeded_user):
    resp = client.post(
        "/api/v1/customers",
        json={"name": "Ana", "email": "no-es-un-email"},
        headers=_auth(seeded_user),
    )

    assert resp.status_code == 422


def test_list_filters_by_search_and_reports_the_total(client, seeded_user):
    for name in ("Ana López", "Bruno Díaz", "Ana María"):
        client.post("/api/v1/customers", json={"name": name}, headers=_auth(seeded_user))

    resp = client.get("/api/v1/customers?search=ana", headers=_auth(seeded_user))

    assert resp.status_code == 200
    body = resp.json()
    assert body["meta"]["total"] == 2
    assert sorted(c["name"] for c in body["data"]) == ["Ana López", "Ana María"]


def test_detail_includes_purchase_history(client, seeded_user):
    created = client.post(
        "/api/v1/customers", json={"name": "Ana"}, headers=_auth(seeded_user)
    ).json()

    resp = client.get(f"/api/v1/customers/{created['id']}", headers=_auth(seeded_user))

    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "Ana"
    assert body["last_purchases"] == []
    assert body["next_purchases"] == []


def test_update_only_touches_the_fields_sent(client, seeded_user):
    created = client.post(
        "/api/v1/customers",
        json={"name": "Ana", "phone": "5555-0000"},
        headers=_auth(seeded_user),
    ).json()

    resp = client.put(
        f"/api/v1/customers/{created['id']}",
        json={"company": "Tienda Ana"},
        headers=_auth(seeded_user),
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["company"] == "Tienda Ana"
    assert body["phone"] == "5555-0000"
    assert body["name"] == "Ana"
