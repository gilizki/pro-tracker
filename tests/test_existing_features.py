"""Regression tests: the features that existed before Friends still behave the same."""
import datetime as dt
import uuid

from conftest import new_username


def test_home_page_and_static_files(client):
    page = client.get("/")
    assert page.status_code == 200 and "Pro Tracker" in page.text
    assert "proTrackerToken" in page.text
    assert client.get("/frontend/icon.PNG").status_code == 200
    script = client.get("/frontend/friends.js")
    assert script.status_code == 200 and "חברים" in script.text


def test_signup_login_me_logout(client):
    username = new_username()
    signup = client.post("/auth/signup", json={"username": username, "password": "password123"})
    assert signup.status_code == 200
    assert client.post("/auth/signup", json={"username": username, "password": "password123"}).status_code == 409

    assert client.post("/auth/login", json={"username": username, "password": "wrong-password"}).status_code == 401
    login = client.post("/auth/login", json={"username": username, "password": "password123"})
    assert login.status_code == 200
    headers = {"Authorization": f"Bearer {login.json()['token']}"}

    me = client.get("/auth/me", headers=headers)
    assert me.status_code == 200 and me.json()["username"] == username
    assert client.post("/auth/logout", headers=headers).json() == {"status": "logged_out"}
    assert client.get("/auth/me", headers=headers).status_code == 401


def test_validation_rules_unchanged(client):
    assert client.post("/auth/signup", json={"username": "ab", "password": "password123"}).status_code == 422
    assert client.post("/auth/signup", json={"username": new_username(), "password": "short"}).status_code == 422


def test_foods_records_summary_and_delete(client, make_user):
    _, headers = make_user()
    _, other_headers = make_user()
    day = dt.date(2026, 1, 15).isoformat()  # existing feature allows any date
    name = f"Food_{uuid.uuid4().hex[:8]}"

    food = client.post("/foods/", json={"name": f"  {name}  ", "calories": 250, "protein": 8}, headers=headers)
    assert food.status_code == 200 and food.json()["name"] == name
    # A user-added food now goes to their personal pool, not a shared catalogue.
    assert not any(f["name"] == name for f in client.get("/foods/", headers=other_headers).json())

    record = client.post("/records/", json={"food_id": food.json()["id"], "amount_grams": 40, "date": day}, headers=headers)
    assert record.status_code == 200
    summary = client.get(f"/summary/{day}", headers=headers).json()
    assert summary["total_calories"] == 100.0 and summary["total_protein"] == 3.2
    assert summary["meals"][0]["food_name"] == name  # the owner still sees their own meals

    # Meals stay private to their owner.
    assert client.get(f"/summary/{day}", headers=other_headers).json()["meals"] == []
    record_id = summary["meals"][0]["record_id"]
    assert client.delete(f"/records/{record_id}", headers=other_headers).status_code == 404
    assert client.delete(f"/records/{record_id}", headers=headers).json() == {"status": "deleted"}
    assert client.get(f"/summary/{day}", headers=headers).json()["meals"] == []

    assert client.post("/records/", json={"food_id": 99999999, "amount_grams": 1, "date": day}, headers=headers).status_code == 404


def test_existing_endpoints_require_login(client):
    for method, path in (("get", "/foods/"), ("post", "/records/"), ("get", "/summary/2026-01-01"),
                         ("delete", "/records/1"), ("get", "/auth/me")):
        assert getattr(client, method)(path).status_code == 401
