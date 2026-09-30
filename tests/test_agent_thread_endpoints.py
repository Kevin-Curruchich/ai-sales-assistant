"""Solo el dueno. Hoy cualquier usuario autenticado puede reanudar cualquier
hilo -- la revision de la rama anterior lo marco y se dejo pasar porque habia
un solo usuario. Con dos, deja de ser aceptable."""

import uuid

from tests.fixtures_http import _auth, _create_thread_as


def test_a_user_cannot_read_another_users_thread(client, seeded_user, second_user):
    ajeno = _create_thread_as(client, second_user, "del otro")

    resp = client.patch(
        f"/api/v1/agent/threads/{ajeno['id']}",
        json={"title": "secuestrado"},
        headers=_auth(seeded_user),
    )

    assert resp.status_code == 404


def test_a_thread_that_does_not_exist_answers_the_same_as_one_that_is_not_yours(
    client, seeded_user, second_user
):
    """El mismo codigo en los dos casos: un 403 revelaria que ese hilo existe."""
    inexistente = uuid.uuid4()
    ajeno = _create_thread_as(client, second_user, "del otro")

    a = client.get(f"/api/v1/agent/threads/{inexistente}", headers=_auth(seeded_user))
    b = client.get(f"/api/v1/agent/threads/{ajeno['id']}", headers=_auth(seeded_user))

    assert a.status_code == b.status_code == 404


def test_listing_shows_only_my_threads(client, seeded_user, second_user):
    _create_thread_as(client, seeded_user, "mio")
    _create_thread_as(client, second_user, "del otro")

    resp = client.get("/api/v1/agent/threads", headers=_auth(seeded_user))

    assert [t["title"] for t in resp.json()] == ["mio"]
