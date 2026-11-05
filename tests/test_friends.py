"""Tests for the friends feature, focused on privacy: a non-friend gets no data,
and a friend gets only daily totals, never meals."""
import datetime as dt
import uuid
from pathlib import Path

import pytest

from app.core import database
from conftest import REAL_DB, TEST_DB

ALLOWED_STATUS_KEYS = {"user_id", "username", "date", "total_calories", "total_protein"}


def today():
    return dt.datetime.now(dt.timezone.utc).date()


def DAY():
    """Required ?date= for the friends endpoints (always inside the +-1 window)."""
    return {"date": today().isoformat()}


def log_meal(client, headers, calories=200.0, protein=10.0, grams=150.0, day=None):
    """Creates a uniquely named food and logs it. Returns the food name."""
    name = f"SecretFood_{uuid.uuid4().hex[:8]}"
    food = client.post("/foods/", json={"name": name, "calories": calories, "protein": protein}, headers=headers)
    assert food.status_code == 200, food.text
    record = client.post("/records/", json={"food_id": food.json()["id"], "amount_grams": grams,
                                            "date": (day or today()).isoformat()}, headers=headers)
    assert record.status_code == 200, record.text
    return name


def befriend(client, a_headers, b_user, b_headers):
    sent = client.post("/friends/requests", json={"username": b_user["username"]}, headers=a_headers)
    assert sent.status_code == 200 and sent.json()["status"] == "pending", sent.text
    incoming = client.get("/friends/requests", headers=b_headers).json()["incoming"]
    request_id = incoming[0]["id"]
    assert client.post(f"/friends/requests/{request_id}/accept", headers=b_headers).status_code == 200


def assert_leaks_nothing(response, food_name):
    text = response.text
    assert food_name not in text
    for forbidden in ("meals", "food", "record", "amount_grams"):
        assert forbidden not in text


# ---------------------------------------------------------------- safety
def test_tests_use_temp_database_not_tracker_db():
    used = Path(database.engine.url.database).resolve()
    assert used == TEST_DB
    assert used != REAL_DB


# ---------------------------------------------------------------- privacy
def test_non_friend_gets_no_data(client, make_user):
    a, a_headers = make_user()
    b, b_headers = make_user()
    food_name = log_meal(client, b_headers)

    stranger = client.get(f"/friends/{b['id']}/status", params=DAY(), headers=a_headers)
    missing = client.get("/friends/99999999/status", params=DAY(), headers=a_headers)
    assert stranger.status_code == 404
    # Indistinguishable from a user that does not exist.
    assert stranger.json() == missing.json() == {"detail": "Friend not found"}
    assert_leaks_nothing(stranger, food_name)

    friends = client.get("/friends", params=DAY(), headers=a_headers)
    assert friends.status_code == 200 and friends.json() == []
    # Removing a non-friend reveals nothing either.
    assert client.delete(f"/friends/{b['id']}", headers=a_headers).json() == {"detail": "Friend not found"}


def test_pending_request_gives_no_data(client, make_user):
    a, a_headers = make_user()
    b, b_headers = make_user()
    food_name = log_meal(client, b_headers)
    assert client.post("/friends/requests", json={"username": b["username"]}, headers=a_headers).status_code == 200

    for viewer_headers, target in ((a_headers, b), (b_headers, a)):
        response = client.get(f"/friends/{target['id']}/status", params=DAY(), headers=viewer_headers)
        assert response.status_code == 404
        assert response.json() == {"detail": "Friend not found"}
        assert_leaks_nothing(response, food_name)
    assert client.get("/friends", params=DAY(), headers=a_headers).json() == []


def test_friend_gets_only_totals_never_meals(client, make_user):
    a, a_headers = make_user()
    b, b_headers = make_user()
    food_name = log_meal(client, b_headers, calories=200, protein=10, grams=150)  # 300 kcal, 15 g
    log_meal(client, b_headers, calories=100, protein=20, grams=50)  # 50 kcal, 10 g
    befriend(client, a_headers, b, b_headers)

    status = client.get(f"/friends/{b['id']}/status", params={"date": today().isoformat()}, headers=a_headers)
    assert status.status_code == 200
    body = status.json()
    assert set(body) == ALLOWED_STATUS_KEYS
    assert body["total_calories"] == 350.0 and body["total_protein"] == 25.0
    assert_leaks_nothing(status, food_name)

    # Same numbers as the owner's own summary (reused calculation, no divergence).
    own = client.get(f"/summary/{today().isoformat()}", headers=b_headers).json()
    assert (body["total_calories"], body["total_protein"]) == (own["total_calories"], own["total_protein"])

    listing = client.get("/friends", params={"date": today().isoformat()}, headers=a_headers)
    assert listing.status_code == 200
    assert listing.json() == [body]
    assert_leaks_nothing(listing, food_name)

    # Works in both directions.
    reverse = client.get(f"/friends/{a['id']}/status", params=DAY(), headers=b_headers)
    assert reverse.status_code == 200 and set(reverse.json()) == ALLOWED_STATUS_KEYS


def test_friend_cannot_browse_other_days(client, make_user):
    a, a_headers = make_user()
    b, b_headers = make_user()
    befriend(client, a_headers, b, b_headers)
    for offset in (-1, 0, 1):
        day = (today() + dt.timedelta(days=offset)).isoformat()
        assert client.get(f"/friends/{b['id']}/status", params={"date": day}, headers=a_headers).status_code == 200
    for offset in (-2, 2, -30):
        day = (today() + dt.timedelta(days=offset)).isoformat()
        assert client.get(f"/friends/{b['id']}/status", params={"date": day}, headers=a_headers).status_code == 400
        assert client.get("/friends", params={"date": day}, headers=a_headers).status_code == 400


def test_requests_list_shows_only_usernames(client, make_user):
    a, a_headers = make_user()
    b, b_headers = make_user()
    log_meal(client, a_headers)
    client.post("/friends/requests", json={"username": b["username"]}, headers=a_headers)
    incoming = client.get("/friends/requests", headers=b_headers).json()
    assert incoming["outgoing"] == []
    assert len(incoming["incoming"]) == 1 and set(incoming["incoming"][0]) == {"id", "username"}
    assert incoming["incoming"][0]["username"] == a["username"]


def test_all_friend_endpoints_require_login(client):
    calls = [
        ("get", "/friends"), ("get", "/friends/1/status"), ("delete", "/friends/1"),
        ("get", "/friends/requests"), ("post", "/friends/requests"),
        ("post", "/friends/requests/1/accept"), ("post", "/friends/requests/1/decline"),
        ("delete", "/friends/requests/1"), ("get", "/friends/invite-code"),
        ("post", "/friends/invite-code/regenerate"),
    ]
    for method, path in calls:
        assert getattr(client, method)(path).status_code == 401, path
        bad = getattr(client, method)(path, headers={"Authorization": "Bearer not-a-real-token"})
        assert bad.status_code == 401, path


# ---------------------------------------------------------------- request flow
def test_username_not_found(client, make_user):
    _, a_headers = make_user()
    response = client.post("/friends/requests", json={"username": "no_such_user_xyz"}, headers=a_headers)
    assert response.status_code == 404 and response.json() == {"detail": "User not found"}


def test_must_send_exactly_one_of_username_or_code(client, make_user):
    b, _ = make_user()
    _, a_headers = make_user()
    assert client.post("/friends/requests", json={}, headers=a_headers).status_code == 422
    both = {"username": b["username"], "invite_code": "ABCD-EFGH"}
    assert client.post("/friends/requests", json=both, headers=a_headers).status_code == 422


def test_cannot_add_yourself(client, make_user):
    a, a_headers = make_user()
    code = client.get("/friends/invite-code", headers=a_headers).json()["code"]
    assert client.post("/friends/requests", json={"username": a["username"]}, headers=a_headers).status_code == 400
    assert client.post("/friends/requests", json={"invite_code": code}, headers=a_headers).status_code == 400
    assert client.get(f"/friends/{a['id']}/status", params=DAY(), headers=a_headers).status_code == 404


def test_duplicate_and_reverse_requests(client, make_user):
    a, a_headers = make_user()
    b, b_headers = make_user()
    assert client.post("/friends/requests", json={"username": b["username"]}, headers=a_headers).status_code == 200
    duplicate = client.post("/friends/requests", json={"username": b["username"]}, headers=a_headers)
    assert duplicate.status_code == 409
    # B sending back to A accepts A's pending request.
    reverse = client.post("/friends/requests", json={"username": a["username"]}, headers=b_headers)
    assert reverse.status_code == 200 and reverse.json()["status"] == "accepted"
    assert client.get(f"/friends/{b['id']}/status", params=DAY(), headers=a_headers).status_code == 200
    again = client.post("/friends/requests", json={"username": b["username"]}, headers=a_headers)
    assert again.status_code == 409 and again.json()["detail"] == "You are already friends"


def test_only_addressee_can_accept_or_decline(client, make_user):
    a, a_headers = make_user()
    b, b_headers = make_user()
    _, c_headers = make_user()
    client.post("/friends/requests", json={"username": b["username"]}, headers=a_headers)
    request_id = client.get("/friends/requests", headers=b_headers).json()["incoming"][0]["id"]

    for headers in (a_headers, c_headers):  # the sender and an outsider
        assert client.post(f"/friends/requests/{request_id}/accept", headers=headers).status_code == 404
        assert client.post(f"/friends/requests/{request_id}/decline", headers=headers).status_code == 404
    assert client.delete(f"/friends/requests/{request_id}", headers=c_headers).status_code == 404
    assert client.get(f"/friends/{b['id']}/status", params=DAY(), headers=a_headers).status_code == 404


def test_decline_then_request_again(client, make_user):
    a, a_headers = make_user()
    b, b_headers = make_user()
    client.post("/friends/requests", json={"username": b["username"]}, headers=a_headers)
    request_id = client.get("/friends/requests", headers=b_headers).json()["incoming"][0]["id"]
    assert client.post(f"/friends/requests/{request_id}/decline", headers=b_headers).status_code == 200
    assert client.get("/friends/requests", headers=b_headers).json()["incoming"] == []
    assert client.get(f"/friends/{b['id']}/status", params=DAY(), headers=a_headers).status_code == 404
    assert client.post("/friends/requests", json={"username": b["username"]}, headers=a_headers).status_code == 200


def test_sender_can_cancel(client, make_user):
    _, a_headers = make_user()
    b, b_headers = make_user()
    client.post("/friends/requests", json={"username": b["username"]}, headers=a_headers)
    request_id = client.get("/friends/requests", headers=a_headers).json()["outgoing"][0]["id"]
    assert client.delete(f"/friends/requests/{request_id}", headers=b_headers).status_code == 404  # not the sender
    assert client.delete(f"/friends/requests/{request_id}", headers=a_headers).status_code == 200
    assert client.get("/friends/requests", headers=b_headers).json()["incoming"] == []


@pytest.mark.parametrize("remover", ["requester", "addressee"])
def test_either_side_can_remove_friend(client, make_user, remover):
    a, a_headers = make_user()
    b, b_headers = make_user()
    befriend(client, a_headers, b, b_headers)
    if remover == "requester":
        assert client.delete(f"/friends/{b['id']}", headers=a_headers).status_code == 200
    else:
        assert client.delete(f"/friends/{a['id']}", headers=b_headers).status_code == 200
    assert client.get(f"/friends/{b['id']}/status", params=DAY(), headers=a_headers).status_code == 404
    assert client.get(f"/friends/{a['id']}/status", params=DAY(), headers=b_headers).status_code == 404
    assert client.get("/friends", params=DAY(), headers=a_headers).json() == []


# ---------------------------------------------------------------- invite codes
def test_invite_code_creates_pending_request(client, make_user):
    a, a_headers = make_user()
    b, b_headers = make_user()
    code = client.get("/friends/invite-code", headers=a_headers).json()["code"]
    assert len(code) == 9 and code[4] == "-"
    assert client.get("/friends/invite-code", headers=a_headers).json()["code"] == code  # permanent

    # Lower-case and without the dash is accepted too.
    sent = client.post("/friends/requests", json={"invite_code": code.replace("-", "").lower()}, headers=b_headers)
    assert sent.status_code == 200 and sent.json() == {"status": "pending", "username": a["username"]}
    # Pending only: no data until the code owner accepts.
    assert client.get(f"/friends/{a['id']}/status", params=DAY(), headers=b_headers).status_code == 404

    request_id = client.get("/friends/requests", headers=a_headers).json()["incoming"][0]["id"]
    client.post(f"/friends/requests/{request_id}/accept", headers=a_headers)
    assert client.get(f"/friends/{a['id']}/status", params=DAY(), headers=b_headers).status_code == 200


def test_regenerate_invalidates_old_code(client, make_user):
    _, a_headers = make_user()
    _, b_headers = make_user()
    old = client.get("/friends/invite-code", headers=a_headers).json()["code"]
    new = client.post("/friends/invite-code/regenerate", headers=a_headers).json()["code"]
    assert new != old
    assert client.get("/friends/invite-code", headers=a_headers).json()["code"] == new

    stale = client.post("/friends/requests", json={"invite_code": old}, headers=b_headers)
    assert stale.status_code == 404 and stale.json() == {"detail": "Invalid invite code"}
    assert client.post("/friends/requests", json={"invite_code": new}, headers=b_headers).status_code == 200


@pytest.mark.parametrize("bad_code", ["ZZZZ-ZZZZ", "abc", "0000-0000", "!!!!-!!!!"])
def test_invalid_invite_codes(client, make_user, bad_code):
    _, a_headers = make_user()
    response = client.post("/friends/requests", json={"invite_code": bad_code}, headers=a_headers)
    assert response.status_code == 404 and response.json() == {"detail": "Invalid invite code"}


def test_date_is_required(client, make_user):
    a, a_headers = make_user()
    b, b_headers = make_user()
    befriend(client, a_headers, b, b_headers)
    assert client.get(f"/friends/{b['id']}/status", headers=a_headers).status_code == 422
    assert client.get("/friends", headers=a_headers).status_code == 422
