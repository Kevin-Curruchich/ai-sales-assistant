from datetime import timedelta

from app.core.datetime_utils import business_today
from app.models.follow_up import FollowUp
from tests.fixtures_http import _auth


def _open_follow_up(db, customer, product):
    follow_up = FollowUp(
        customer_id=customer.id,
        product_id=product.id,
        due_date=business_today() + timedelta(days=2),
        status="open",
    )
    db.add(follow_up)
    db.commit()
    return follow_up


def test_postpone_returns_the_new_follow_up(
    client, db_session, seeded_user, seeded_customer, seeded_product_with_lot
):
    follow_up = _open_follow_up(db_session, seeded_customer, seeded_product_with_lot)
    new_date = business_today() + timedelta(days=9)

    resp = client.post(
        f"/api/v1/follow-ups/{follow_up.id}/postpone",
        json={"date": new_date.isoformat()},
        headers=_auth(seeded_user),
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["due_date"] == new_date.isoformat()
    assert body["status"] == "open"
    assert body["id"] != str(follow_up.id)


def test_postpone_to_today_is_unprocessable(
    client, db_session, seeded_user, seeded_customer, seeded_product_with_lot
):
    follow_up = _open_follow_up(db_session, seeded_customer, seeded_product_with_lot)

    resp = client.post(
        f"/api/v1/follow-ups/{follow_up.id}/postpone",
        json={"date": business_today().isoformat()},
        headers=_auth(seeded_user),
    )

    assert resp.status_code == 422


def test_dismiss_twice_is_a_conflict(
    client, db_session, seeded_user, seeded_customer, seeded_product_with_lot
):
    follow_up = _open_follow_up(db_session, seeded_customer, seeded_product_with_lot)
    url = f"/api/v1/follow-ups/{follow_up.id}/dismiss"

    assert client.post(url, headers=_auth(seeded_user)).status_code == 204
    assert client.post(url, headers=_auth(seeded_user)).status_code == 409


def test_actions_require_authentication(
    client, db_session, seeded_customer, seeded_product_with_lot
):
    follow_up = _open_follow_up(db_session, seeded_customer, seeded_product_with_lot)

    resp = client.post(f"/api/v1/follow-ups/{follow_up.id}/dismiss")

    assert resp.status_code in (401, 403)
