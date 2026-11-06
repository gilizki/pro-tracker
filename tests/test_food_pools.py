"""Global vs personal food pools: visibility, ownership, admin-only actions and
the suggest -> approve / reject flows. Every check goes through the HTTP API, so
these test the server-side rules, not the UI."""
import uuid

DAY = "2026-01-15"


def unique(prefix="Food"):
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


def add_food(client, headers, name=None, calories=100.0, protein=5.0, **extra):
    response = client.post("/foods/", json={"name": name or unique(), "calories": calories, "protein": protein, **extra},
                           headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def visible_ids(client, headers):
    return {food["id"] for food in client.get("/foods/", headers=headers).json()}


def log(client, headers, food_id, grams=100):
    return client.post("/records/", json={"food_id": food_id, "amount_grams": grams, "date": DAY}, headers=headers)


def edit(client, headers, food, **changes):
    body = {"name": food["name"], "calories": food["calories"], "protein": food["protein"], **changes}
    return client.put(f"/foods/{food['id']}", json=body, headers=headers)


# ---------------------------------------------------------------- roles

def test_me_reports_admin_flag(client, make_user, make_admin):
    user, headers = make_user()
    admin, admin_headers = make_admin()
    assert client.get("/auth/me", headers=headers).json()["is_admin"] is False
    assert client.get("/auth/me", headers=admin_headers).json()["is_admin"] is True
    login = client.post("/auth/login", json={"username": user["username"], "password": "password123"})
    assert login.json()["user"]["is_admin"] is False


def test_new_endpoints_require_login(client):
    for method, path in (("put", "/foods/1"), ("delete", "/foods/1"), ("get", "/admin/suggestions"),
                         ("post", "/admin/suggestions/1/approve"), ("post", "/admin/suggestions/1/reject")):
        kwargs = {"json": {"name": "x", "calories": 1, "protein": 1}} if method == "put" else {}
        assert getattr(client, method)(path, **kwargs).status_code == 401


# ---------------------------------------------------------------- visibility

def test_visibility_global_plus_own_personal_only(client, make_user, make_admin):
    _, a = make_user()
    _, b = make_user()
    _, admin = make_admin()
    mine = add_food(client, a)
    theirs = add_food(client, b)
    shared = add_food(client, admin, is_global=True)

    assert mine["scope"] == "personal" and mine["can_edit"] is True and mine["suggestion_status"] is None
    assert shared["scope"] == "global"

    a_ids, b_ids, admin_ids = visible_ids(client, a), visible_ids(client, b), visible_ids(client, admin)
    assert mine["id"] in a_ids and theirs["id"] not in a_ids
    assert theirs["id"] in b_ids and mine["id"] not in b_ids
    assert shared["id"] in a_ids and shared["id"] in b_ids
    # Admins do not see other users' personal foods either.
    assert mine["id"] not in admin_ids and theirs["id"] not in admin_ids

    listed = {food["id"]: food for food in client.get("/foods/", headers=a).json()}
    assert listed[shared["id"]]["scope"] == "global" and listed[shared["id"]]["can_edit"] is False
    assert listed[mine["id"]]["scope"] == "personal"
    assert {food["id"]: food for food in client.get("/foods/", headers=admin).json()}[shared["id"]]["can_edit"] is True


def test_pending_suggestion_is_still_private(client, make_user):
    _, a = make_user()
    _, b = make_user()
    food = add_food(client, a, suggest=True)
    assert food["suggestion_status"] == "pending" and food["scope"] == "personal"
    assert food["id"] in visible_ids(client, a)
    assert food["id"] not in visible_ids(client, b)


# ---------------------------------------------------------------- ownership

def test_cannot_touch_another_users_personal_food(client, make_user, make_admin):
    _, a = make_user()
    _, b = make_user()
    _, admin = make_admin()
    food = add_food(client, a, calories=123)

    for headers in (b, admin):
        assert edit(client, headers, food, calories=1).status_code == 404
        assert client.delete(f"/foods/{food['id']}", headers=headers).status_code == 404
        assert log(client, headers, food["id"]).status_code == 404

    still = {f["id"]: f for f in client.get("/foods/", headers=a).json()}[food["id"]]
    assert still["calories"] == 123
    assert log(client, a, food["id"]).status_code == 200


def test_owner_can_edit_and_delete_personal_food(client, make_user):
    _, a = make_user()
    food = add_food(client, a)
    updated = edit(client, a, food, name=food["name"] + "_v2", calories=321)
    assert updated.status_code == 200 and updated.json()["calories"] == 321
    assert client.delete(f"/foods/{food['id']}", headers=a).json() == {"status": "deleted", "records_deleted": 0}
    assert food["id"] not in visible_ids(client, a)


def test_blank_name_rejected(client, make_user):
    _, a = make_user()
    assert client.post("/foods/", json={"name": "   ", "calories": 1, "protein": 1}, headers=a).status_code == 422


# ---------------------------------------------------------------- admin-only

def test_non_admin_cannot_manage_global_foods_or_suggestions(client, make_user, make_admin):
    _, a = make_user()
    _, admin = make_admin()
    shared = add_food(client, admin, is_global=True)
    suggestion = add_food(client, a, suggest=True)

    assert client.post("/foods/", json={"name": unique(), "calories": 1, "protein": 1, "is_global": True},
                       headers=a).status_code == 403
    assert edit(client, a, shared, calories=999).status_code == 403
    assert client.delete(f"/foods/{shared['id']}", headers=a).status_code == 403
    assert client.get("/admin/suggestions", headers=a).status_code == 403
    assert client.post(f"/admin/suggestions/{suggestion['id']}/approve", headers=a).status_code == 403
    assert client.post(f"/admin/suggestions/{suggestion['id']}/reject", headers=a).status_code == 403

    # Nothing changed.
    assert {f["id"]: f for f in client.get("/foods/", headers=a).json()}[shared["id"]]["calories"] == shared["calories"]
    assert {f["id"]: f for f in client.get("/foods/", headers=a).json()}[suggestion["id"]]["suggestion_status"] == "pending"


def test_admin_manages_global_foods(client, make_admin, make_user):
    _, admin = make_admin()
    _, a = make_user()
    shared = add_food(client, admin, is_global=True)
    assert edit(client, admin, shared, calories=55).json()["calories"] == 55
    assert {f["id"]: f for f in client.get("/foods/", headers=a).json()}[shared["id"]]["calories"] == 55
    assert client.delete(f"/foods/{shared['id']}", headers=admin).status_code == 200
    assert shared["id"] not in visible_ids(client, a)
    # A global food cannot also be a suggestion.
    assert client.post("/foods/", json={"name": unique(), "calories": 1, "protein": 1, "is_global": True, "suggest": True},
                       headers=admin).status_code == 422


# ---------------------------------------------------------------- approve / reject

def test_approve_keeps_id_and_existing_records(client, make_user, make_admin):
    owner, a = make_user()
    _, b = make_user()
    _, admin = make_admin()
    food = add_food(client, a, calories=200, protein=10, suggest=True)
    assert log(client, a, food["id"], grams=50).status_code == 200

    pending = {s["id"]: s for s in client.get("/admin/suggestions", headers=admin).json()}
    assert pending[food["id"]]["owner_username"] == owner["username"]
    assert pending[food["id"]]["duplicate_of_global_id"] is None

    approved = client.post(f"/admin/suggestions/{food['id']}/approve", headers=admin)
    assert approved.status_code == 200
    assert approved.json()["id"] == food["id"]
    assert approved.json()["scope"] == "global" and approved.json()["suggestion_status"] is None

    # Same id, now visible to everyone; the owner's existing meal still resolves.
    assert food["id"] in visible_ids(client, b)
    summary = client.get(f"/summary/{DAY}", headers=a).json()
    assert [m["food_name"] for m in summary["meals"]] == [food["name"]] and summary["total_calories"] == 100.0
    assert log(client, b, food["id"]).status_code == 200
    # It is global now: only admins may change it, and it is no longer pending.
    assert edit(client, a, food, calories=1).status_code == 403
    assert food["id"] not in {s["id"] for s in client.get("/admin/suggestions", headers=admin).json()}
    assert client.post(f"/admin/suggestions/{food['id']}/approve", headers=admin).status_code == 404
    assert client.post(f"/admin/suggestions/{food['id']}/reject", headers=admin).status_code == 404


def test_reject_keeps_food_personal(client, make_user, make_admin):
    _, a = make_user()
    _, b = make_user()
    _, admin = make_admin()
    food = add_food(client, a, suggest=True)
    assert log(client, a, food["id"]).status_code == 200

    assert client.post(f"/admin/suggestions/{food['id']}/reject", headers=admin).json() == {"status": "rejected"}
    mine = {f["id"]: f for f in client.get("/foods/", headers=a).json()}
    assert mine[food["id"]]["scope"] == "personal" and mine[food["id"]]["suggestion_status"] == "rejected"
    assert food["id"] not in visible_ids(client, b)
    assert food["id"] not in {s["id"] for s in client.get("/admin/suggestions", headers=admin).json()}
    assert client.get(f"/summary/{DAY}", headers=a).json()["meals"][0]["food_name"] == food["name"]
    assert client.post(f"/admin/suggestions/{food['id']}/approve", headers=admin).status_code == 404


def test_resuggest_rejected_food_only_after_editing(client, make_user, make_admin):
    _, a = make_user()
    _, admin = make_admin()
    food = add_food(client, a, suggest=True)
    client.post(f"/admin/suggestions/{food['id']}/reject", headers=admin)

    unchanged = edit(client, a, food, suggest=True)
    assert unchanged.status_code == 409 and unchanged.json()["detail"]["code"] == "edit_before_resuggest"

    resuggested = edit(client, a, food, calories=food["calories"] + 1, suggest=True)
    assert resuggested.status_code == 200 and resuggested.json()["suggestion_status"] == "pending"
    assert food["id"] in {s["id"] for s in client.get("/admin/suggestions", headers=admin).json()}


def test_owner_can_withdraw_pending_suggestion(client, make_user, make_admin):
    _, a = make_user()
    _, admin = make_admin()
    food = add_food(client, a, suggest=True)
    withdrawn = edit(client, a, food, suggest=False)
    assert withdrawn.status_code == 200 and withdrawn.json()["suggestion_status"] is None
    assert food["id"] not in {s["id"] for s in client.get("/admin/suggestions", headers=admin).json()}


# ---------------------------------------------------------------- deleting a food together with its log entries

def summary(client, headers, day=DAY):
    return client.get(f"/summary/{day}", headers=headers).json()


def usage(client, headers, food_id):
    return client.get(f"/foods/{food_id}/usage", headers=headers)


def delete(client, headers, food_id, confirm_records=None):
    query = "" if confirm_records is None else f"?confirm_records={confirm_records}"
    return client.delete(f"/foods/{food_id}{query}", headers=headers)


def test_unused_food_is_deleted_without_confirmation(client, make_user, make_admin):
    _, a = make_user()
    _, admin = make_admin()
    personal, shared = add_food(client, a), add_food(client, admin, is_global=True)
    assert usage(client, a, personal["id"]).json() == {"food_id": personal["id"], "scope": "personal", "users": 0, "records": 0}
    assert usage(client, admin, shared["id"]).json() == {"food_id": shared["id"], "scope": "global", "users": 0, "records": 0}
    assert delete(client, a, personal["id"]).json() == {"status": "deleted", "records_deleted": 0}
    assert delete(client, admin, shared["id"]).json() == {"status": "deleted", "records_deleted": 0}
    assert personal["id"] not in visible_ids(client, a) and shared["id"] not in visible_ids(client, a)


def test_owner_deletes_used_personal_food_and_its_entries(client, make_user):
    _, a = make_user()
    food = add_food(client, a, calories=200, protein=10)
    keep = add_food(client, a, calories=100, protein=1)
    log(client, a, food["id"], grams=50)
    log(client, a, keep["id"], grams=100)
    client.post("/records/", json={"food_id": food["id"], "amount_grams": 100, "date": "2026-01-16"}, headers=a)
    assert usage(client, a, food["id"]).json()["records"] == 2

    # Not confirmed (or confirmed with a stale count): nothing changes, the server returns the real numbers.
    for confirm in (None, 1):
        refused = delete(client, a, food["id"], confirm)
        assert refused.status_code == 409
        assert refused.json()["detail"] | {"message": ""} == {"code": "food_in_use_confirm", "users": 1, "records": 2, "message": ""}
    assert summary(client, a)["total_calories"] == 200

    assert delete(client, a, food["id"], 2).json() == {"status": "deleted", "records_deleted": 2}
    assert food["id"] not in visible_ids(client, a)
    today = summary(client, a)
    assert [m["food_name"] for m in today["meals"]] == [keep["name"]]  # the other meal of that day stays
    assert (today["total_calories"], today["total_protein"]) == (100, 1)  # totals recomputed
    assert summary(client, a, "2026-01-16")["meals"] == []


def test_admin_deletes_used_global_food_for_every_user(client, make_admin, make_user):
    _, admin = make_admin()
    users = [make_user()[1] for _ in range(3)]
    shared = add_food(client, admin, is_global=True, calories=200, protein=10)
    for headers, grams in ((users[0], 100), (users[1], 30), (users[1], 70), (admin, 50)):
        log(client, headers, shared["id"], grams=grams)
    bystander_food = add_food(client, users[2])
    log(client, users[2], bystander_food["id"])

    assert usage(client, admin, shared["id"]).json() == {"food_id": shared["id"], "scope": "global", "users": 3, "records": 4}
    assert delete(client, admin, shared["id"], 4).json() == {"status": "deleted", "records_deleted": 4}

    for headers in users + [admin]:
        assert shared["id"] not in visible_ids(client, headers)
        assert all(m["food_name"] != shared["name"] for m in summary(client, headers)["meals"])
    assert summary(client, users[0])["total_calories"] == 0
    assert len(summary(client, users[2])["meals"]) == 1  # other foods are untouched


def test_deleting_a_pending_suggestion_removes_it_from_the_admin_queue(client, make_user, make_admin):
    _, a = make_user()
    _, admin = make_admin()
    suggestion = add_food(client, a, suggest=True)
    log(client, a, suggestion["id"])
    assert suggestion["id"] in {s["id"] for s in client.get("/admin/suggestions", headers=admin).json()}
    assert delete(client, a, suggestion["id"], 1).status_code == 200
    assert suggestion["id"] not in {s["id"] for s in client.get("/admin/suggestions", headers=admin).json()}
    assert client.post(f"/admin/suggestions/{suggestion['id']}/approve", headers=admin).status_code == 404


def test_only_admins_delete_global_foods_or_see_their_usage(client, make_admin, make_user):
    _, admin = make_admin()
    _, a = make_user()
    shared = add_food(client, admin, is_global=True)
    log(client, a, shared["id"])
    assert delete(client, a, shared["id"], 1).status_code == 403
    assert usage(client, a, shared["id"]).status_code == 403
    assert shared["id"] in visible_ids(client, a) and len(summary(client, a)["meals"]) == 1


def test_nobody_deletes_another_users_personal_food(client, make_admin, make_user):
    _, admin = make_admin()
    _, a = make_user()
    _, b = make_user()
    personal = add_food(client, a)
    log(client, a, personal["id"])
    for headers in (admin, b):  # 404, not 403: other users' personal foods do not exist for them
        assert delete(client, headers, personal["id"], 1).status_code == 404
        assert usage(client, headers, personal["id"]).status_code == 404
    assert personal["id"] in visible_ids(client, a) and len(summary(client, a)["meals"]) == 1


def test_delete_rolls_back_everything_on_failure(client, make_user, monkeypatch):
    import pytest
    from sqlalchemy.orm import Query

    from app.core import database
    from app.tracker.models import Food, UserRecord

    _, a = make_user()
    food = add_food(client, a)
    log(client, a, food["id"])
    log(client, a, food["id"])
    before = summary(client, a)

    real_delete, calls = Query.delete, []

    def delete_then_fail(self, *args, **kwargs):
        entity = self.column_descriptions[0]["entity"]
        calls.append(entity)
        if entity is Food:  # the records were already deleted inside this transaction
            raise RuntimeError("simulated failure")
        return real_delete(self, *args, **kwargs)

    monkeypatch.setattr(Query, "delete", delete_then_fail)
    with pytest.raises(RuntimeError, match="simulated failure"):
        delete(client, a, food["id"], 2)
    monkeypatch.undo()
    assert calls == [UserRecord, Food]

    with database.SessionLocal() as db:
        assert db.get(Food, food["id"]) is not None
        assert db.query(UserRecord).filter(UserRecord.food_id == food["id"]).count() == 2
    assert summary(client, a) == before
    assert delete(client, a, food["id"], 2).json()["records_deleted"] == 2  # a retry works


# ---------------------------------------------------------------- duplicates

def test_personal_duplicate_of_global_name_warns_then_allows(client, make_user, make_admin):
    _, a = make_user()
    _, admin = make_admin()
    name = unique("Banana")
    shared = add_food(client, admin, name=name, is_global=True)

    warned = client.post("/foods/", json={"name": f"  {name.upper()} ", "calories": 1, "protein": 1}, headers=a)
    assert warned.status_code == 409
    assert warned.json()["detail"]["code"] == "duplicate_global" and warned.json()["detail"]["food_id"] == shared["id"]

    confirmed = add_food(client, a, name=name, allow_duplicate=True)
    assert confirmed["scope"] == "personal" and confirmed["id"] != shared["id"]

    # Renaming a personal food to a global name warns too (after removing the copy, which would be a personal duplicate).
    assert client.delete(f"/foods/{confirmed['id']}", headers=a).status_code == 200
    other = add_food(client, a)
    assert edit(client, a, other, name=name).json()["detail"]["code"] == "duplicate_global"
    assert edit(client, a, other, name=name, allow_duplicate=True).status_code == 200


def test_suggesting_global_name_is_blocked(client, make_user, make_admin):
    _, a = make_user()
    _, admin = make_admin()
    name = unique("Rice")
    add_food(client, admin, name=name, is_global=True)

    blocked = client.post("/foods/", json={"name": name.lower(), "calories": 1, "protein": 1, "suggest": True,
                                           "allow_duplicate": True}, headers=a)
    assert blocked.status_code == 409 and blocked.json()["detail"]["code"] == "duplicate_global_suggestion"

    personal = add_food(client, a, name=name, allow_duplicate=True)
    assert edit(client, a, personal, suggest=True).json()["detail"]["code"] == "duplicate_global_suggestion"
    # Admins cannot create a second global food with the same name either.
    assert client.post("/foods/", json={"name": name, "calories": 1, "protein": 1, "is_global": True},
                       headers=admin).status_code == 409


def test_approve_blocked_if_global_name_appeared_meanwhile(client, make_user, make_admin):
    _, a = make_user()
    _, admin = make_admin()
    name = unique("Oats")
    suggestion = add_food(client, a, name=name, suggest=True)
    shared = add_food(client, admin, name=name, is_global=True)

    listed = {s["id"]: s for s in client.get("/admin/suggestions", headers=admin).json()}
    assert listed[suggestion["id"]]["duplicate_of_global_id"] == shared["id"]
    assert client.post(f"/admin/suggestions/{suggestion['id']}/approve", headers=admin).status_code == 409
    assert client.post(f"/admin/suggestions/{suggestion['id']}/reject", headers=admin).status_code == 200


def test_second_personal_food_with_the_same_name_is_blocked(client, make_user):
    _, a = make_user()
    _, b = make_user()
    name = unique("Shake")
    first = add_food(client, a, name=name)
    for body in ({"name": f" {name.upper()}  "}, {"name": name, "allow_duplicate": True}, {"name": name, "suggest": True}):
        blocked = client.post("/foods/", json={"calories": 1, "protein": 1, **body}, headers=a)
        assert blocked.status_code == 409 and blocked.json()["detail"] == {
            "code": "duplicate_personal", "food_id": first["id"], "message": f'You already have a personal food named "{name}".'}
    # Renaming another food to that name is blocked too; editing the food itself (same name) is not.
    other = add_food(client, a)
    assert edit(client, a, other, name=name).json()["detail"]["code"] == "duplicate_personal"
    assert edit(client, a, first, calories=999).status_code == 200
    # Another user may use the same name.
    assert add_food(client, b, name=name)["scope"] == "personal"
