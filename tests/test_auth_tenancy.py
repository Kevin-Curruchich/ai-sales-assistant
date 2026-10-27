import uuid

import pytest
from sqlalchemy import select

from app.models import Business, User
from app.repositories.user_repository import UserRepository
from tests.fixtures_http import _auth

SIGNUP = {"business_name": "Tienda A", "email": "a@a.com", "password": "secreto123"}


@pytest.fixture
def fake_firebase(monkeypatch):
    calls = {"created": [], "deleted": [], "claims": [], "links": []}

    class _Rec:
        def __init__(self, uid):
            self.uid = uid

    def create(uid, email, password=None, display_name=None):
        calls["created"].append({"uid": uid, "email": email, "password": password})
        return _Rec(uid)

    monkeypatch.setattr("app.services.user_service.create_firebase_user", create)
    monkeypatch.setattr(
        "app.services.user_service.delete_firebase_user",
        lambda uid: calls["deleted"].append(uid),
    )
    monkeypatch.setattr(
        "app.services.user_service.set_firebase_custom_claims",
        lambda uid, claims: calls["claims"].append((uid, claims)),
    )

    def link(email):
        calls["links"].append(email)
        return f"https://fb.example/reset?e={email}"

    monkeypatch.setattr("app.services.user_service.generate_password_setup_link", link)
    return calls


def _boom(*_a, **_k):
    raise RuntimeError("fallo de BD")


def test_signup_creates_business_and_admin(client, fake_firebase, db_session):
    r = client.post("/api/v1/auth/signup", json=SIGNUP)
    assert r.status_code == 201 and r.json()["role"] == "admin"
    user = db_session.get(User, uuid.UUID(r.json()["id"]))
    assert user.business.name == "Tienda A"
    assert fake_firebase["claims"][0][1] == {"role": "admin", "is_active": True}


@pytest.mark.parametrize("role", ["admin", "viewer"])
def test_signup_ignores_role_field(client, fake_firebase, role):
    r = client.post("/api/v1/auth/signup", json={**SIGNUP, "role": role})
    assert r.status_code == 201 and r.json()["role"] == "admin"


def test_signup_rolls_back_firebase_on_db_error(
    client, fake_firebase, monkeypatch, db_session
):
    monkeypatch.setattr(UserRepository, "create", _boom)
    with pytest.raises(RuntimeError):
        client.post("/api/v1/auth/signup", json=SIGNUP)
    uid = fake_firebase["created"][0]["uid"]
    assert fake_firebase["deleted"] == [uid]
    db_session.rollback()
    assert not db_session.scalars(
        select(Business).where(Business.name == "Tienda A")
    ).all()


def test_admin_creates_seller_without_password(client, fake_firebase, seeded_user):
    r = client.post(
        "/api/v1/auth/users",
        json={"email": "v@a.com", "role": "sales_rep"},
        headers=_auth(seeded_user),
    )
    assert r.status_code == 201
    assert fake_firebase["created"][0]["password"] is None
    assert r.json()["password_setup_link"].startswith("https://")
    assert r.json()["role"] == "sales_rep"


def test_admin_cannot_create_admin(client, fake_firebase, seeded_user):
    r = client.post(
        "/api/v1/auth/users",
        json={"email": "v@a.com", "role": "admin"},
        headers=_auth(seeded_user),
    )
    assert r.status_code == 422


def test_seller_creation_rolls_back_firebase(
    client, fake_firebase, seeded_user, monkeypatch
):
    monkeypatch.setattr(UserRepository, "create", _boom)
    with pytest.raises(RuntimeError):
        client.post(
            "/api/v1/auth/users",
            json={"email": "v@a.com", "role": "viewer"},
            headers=_auth(seeded_user),
        )
    assert fake_firebase["deleted"] == [fake_firebase["created"][0]["uid"]]


@pytest.fixture
def foreign_user(db_session, other_business):
    user = User(email="ajeno@x.com", role="viewer", business_id=other_business.id)
    db_session.add(user)
    db_session.commit()
    return user


def test_password_link_for_other_business_is_404(
    client, fake_firebase, seeded_user, foreign_user
):
    r = client.post(
        f"/api/v1/auth/users/{foreign_user.id}/password-link",
        headers=_auth(seeded_user),
    )
    assert r.status_code == 404
    assert fake_firebase["links"] == []


def test_password_link_for_own_user(client, fake_firebase, seeded_user):
    r = client.post(
        f"/api/v1/auth/users/{seeded_user.id}/password-link",
        headers=_auth(seeded_user),
    )
    assert r.status_code == 200 and "password_setup_link" in r.json()


def test_admin_lists_only_own_business_users(client, seeded_user, foreign_user):
    r = client.get("/api/v1/auth/users", headers=_auth(seeded_user))
    assert [u["id"] for u in r.json()] == [str(seeded_user.id)]


def test_admin_cannot_update_user_of_other_business(
    client, fake_firebase, seeded_user, foreign_user
):
    h = _auth(seeded_user)
    r = client.put(
        f"/api/v1/auth/users/{foreign_user.id}", json={"display_name": "x"}, headers=h
    )
    assert r.status_code == 404
    r = client.patch(
        f"/api/v1/auth/users/{foreign_user.id}/role", json={"role": "viewer"}, headers=h
    )
    assert r.status_code == 404


def test_update_role_accepts_sales_rep(client, fake_firebase, seeded_user, db_session):
    v = User(email="v2@a.com", role="viewer", business_id=seeded_user.business_id)
    db_session.add(v)
    db_session.commit()
    r = client.patch(
        f"/api/v1/auth/users/{v.id}/role",
        json={"role": "sales_rep"},
        headers=_auth(seeded_user),
    )
    assert r.status_code == 200 and r.json()["role"] == "sales_rep"


def test_sales_rep_cannot_create_users(client, fake_firebase, db_session, seeded_user):
    rep = User(email="rep@a.com", role="sales_rep", business_id=seeded_user.business_id)
    db_session.add(rep)
    db_session.commit()
    r = client.post(
        "/api/v1/auth/users",
        json={"email": "v@a.com", "role": "viewer"},
        headers=_auth(rep),
    )
    assert r.status_code == 403
