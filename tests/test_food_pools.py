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
    assert client.delete(f"/foods/{food['id']}", headers=a).json() == {"status": "deleted"}
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


# ---------------------------------------------------------------- deletion safety

def test_delete_blocked_while_records_reference_food(client, make_user, make_admin):
    _, a = make_user()
    _, b = make_user()
    _, admin = make_admin()
    personal = add_food(client, a)
    log(client, a, personal["id"])
    blocked = client.delete(f"/foods/{personal['id']}", headers=a)
    assert blocked.status_code == 409 and blocked.json()["detail"]["code"] == "food_in_use"
    assert personal["id"] in visible_ids(client, a)

    record_id = client.get(f"/summary/{DAY}", headers=a).json()["meals"][0]["record_id"]
    client.delete(f"/records/{record_id}", headers=a)
    assert client.delete(f"/foods/{personal['id']}", headers=a).status_code == 200

    # A global food that someone logged is not deleted by a plain DELETE, even by an admin: the server
    # answers with the real counts and asks for confirmation (keep_personal_copies=true, tested below).
    shared = add_food(client, admin, is_global=True)
    log(client, b, shared["id"])
    guarded = client.delete(f"/foods/{shared['id']}", headers=admin)
    assert guarded.status_code == 409
    assert guarded.json()["detail"] | {"message": ""} == {"code": "food_in_use_global", "users": 1, "records": 1, "message": ""}
    assert shared["id"] in visible_ids(client, b)


# ---------------------------------------------------------------- admin deletes a global food that has records

def summary(client, headers, day=DAY):
    return client.get(f"/summary/{day}", headers=headers).json()


def personal_named(client, headers, name):
    return [f for f in client.get("/foods/", headers=headers).json() if f["name"] == name and f["scope"] == "personal"]


def delete_keeping_copies(client, headers, food_id):
    return client.delete(f"/foods/{food_id}?keep_personal_copies=true", headers=headers)


def test_admin_deletes_unused_global_food_plainly(client, make_admin, make_user):
    _, admin = make_admin()
    _, a = make_user()
    shared = add_food(client, admin, is_global=True)
    assert client.get(f"/admin/foods/{shared['id']}/usage", headers=admin).json() == {"food_id": shared["id"], "users": 0, "records": 0}
    deleted = client.delete(f"/foods/{shared['id']}", headers=admin)
    assert deleted.json() == {"status": "deleted", "personal_copies": 0, "records_moved": 0}
    assert shared["id"] not in visible_ids(client, a)
    assert personal_named(client, a, shared["name"]) == []


def test_admin_deletes_global_food_used_by_one_user(client, make_admin, make_user):
    _, admin = make_admin()
    _, a = make_user()
    _, bystander = make_user()
    shared = add_food(client, admin, is_global=True, calories=120.5, protein=7.25)
    log(client, a, shared["id"], grams=150)
    log(client, a, shared["id"], grams=50)
    before = summary(client, a)
    assert client.get(f"/admin/foods/{shared['id']}/usage", headers=admin).json() == {"food_id": shared["id"], "users": 1, "records": 2}

    result = delete_keeping_copies(client, admin, shared["id"])
    assert result.status_code == 200 and result.json() == {"status": "deleted", "personal_copies": 1, "records_moved": 2}

    for headers in (a, bystander, admin):
        assert shared["id"] not in visible_ids(client, headers)  # gone from the shared pool for everyone
    [copy] = personal_named(client, a, shared["name"])
    assert (copy["calories"], copy["protein"], copy["suggestion_status"], copy["can_edit"]) == (120.5, 7.25, None, True)
    assert personal_named(client, bystander, shared["name"]) == []  # only people who ate it get a copy
    assert personal_named(client, admin, shared["name"]) == []
    assert summary(client, a) == before  # same meals, same names, same totals


def test_admin_deletes_global_food_used_by_several_users(client, make_admin, make_user):
    _, admin = make_admin()
    users = [make_user()[1] for _ in range(3)]
    shared = add_food(client, admin, is_global=True, calories=200, protein=10)
    log(client, users[0], shared["id"], grams=100)
    log(client, users[1], shared["id"], grams=30)
    log(client, users[1], shared["id"], grams=70)
    log(client, users[2], shared["id"], grams=10)
    log(client, admin, shared["id"], grams=250)  # the admin ate it too
    other_day = client.post("/records/", json={"food_id": shared["id"], "amount_grams": 40, "date": "2026-01-16"}, headers=users[0])
    assert other_day.status_code == 200
    everyone = users + [admin]
    before = {i: (summary(client, h), summary(client, h, "2026-01-16")) for i, h in enumerate(everyone)}
    assert client.get(f"/admin/foods/{shared['id']}/usage", headers=admin).json()["users"] == 4

    result = delete_keeping_copies(client, admin, shared["id"])
    assert result.json() == {"status": "deleted", "personal_copies": 4, "records_moved": 6}

    copy_ids = set()
    for i, headers in enumerate(everyone):
        [copy] = personal_named(client, headers, shared["name"])  # each user has exactly their own copy
        copy_ids.add(copy["id"])
        assert (summary(client, headers), summary(client, headers, "2026-01-16")) == before[i]
        # the records resolve to the copy: deleting the copy is now blocked as "in use" for its owner
        assert client.delete(f"/foods/{copy['id']}", headers=headers).json()["detail"]["code"] == "food_in_use"
        # and the user can keep logging it
        assert log(client, headers, copy["id"]).status_code == 200
    assert len(copy_ids) == 4
    # nobody can see (or log) anyone else's copy
    for headers in everyone:
        assert len(copy_ids & visible_ids(client, headers)) == 1


def test_copy_is_created_even_if_user_has_a_personal_food_with_that_name(client, make_admin, make_user):
    _, admin = make_admin()
    _, a = make_user()
    shared = add_food(client, admin, is_global=True, calories=100)
    own = add_food(client, a, name=shared["name"], calories=50, allow_duplicate=True)
    log(client, a, shared["id"])
    log(client, a, own["id"])
    before = summary(client, a)

    assert delete_keeping_copies(client, admin, shared["id"]).json()["personal_copies"] == 1
    foods = personal_named(client, a, shared["name"])
    assert sorted(f["calories"] for f in foods) == [50, 100] and own["id"] in {f["id"] for f in foods}
    assert summary(client, a) == before


def test_delete_with_copies_rolls_back_everything_on_failure(client, make_admin, make_user, monkeypatch):
    import pytest

    from app.core import database
    from app.tracker.models import Food, UserRecord
    from app.tracker.repositories import FoodRepository

    _, admin = make_admin()
    _, a = make_user()
    _, b = make_user()
    shared = add_food(client, admin, is_global=True)
    log(client, a, shared["id"])
    log(client, b, shared["id"])
    before = (summary(client, a), summary(client, b))

    real_copy, calls = FoodRepository._personal_copy, []

    def copy_then_fail(db, food, user_id):
        calls.append(user_id)
        if len(calls) == 2:  # the first copy and its record move already happened inside the transaction
            raise RuntimeError("simulated failure")
        return real_copy(db, food, user_id)

    monkeypatch.setattr(FoodRepository, "_personal_copy", staticmethod(copy_then_fail))
    with pytest.raises(RuntimeError, match="simulated failure"):
        delete_keeping_copies(client, admin, shared["id"])
    monkeypatch.undo()
    assert len(calls) == 2

    with database.SessionLocal() as db:
        assert db.get(Food, shared["id"]) is not None  # the global food is still there
        assert db.query(Food).filter(Food.name == shared["name"], Food.owner_id.isnot(None)).count() == 0  # no copies
        assert {r.food_id for r in db.query(UserRecord).filter(UserRecord.user_id.in_(
            [client.get("/auth/me", headers=h).json()["id"] for h in (a, b)]))} == {shared["id"]}
    assert shared["id"] in visible_ids(client, a) and (summary(client, a), summary(client, b)) == before
    # and a retry without the failure works
    assert delete_keeping_copies(client, admin, shared["id"]).json()["personal_copies"] == 2


def test_only_admins_delete_global_foods_or_see_usage(client, make_admin, make_user):
    _, admin = make_admin()
    _, a = make_user()
    shared = add_food(client, admin, is_global=True)
    log(client, a, shared["id"])
    assert client.delete(f"/foods/{shared['id']}?keep_personal_copies=true", headers=a).status_code == 403
    assert client.get(f"/admin/foods/{shared['id']}/usage", headers=a).status_code == 403
    assert shared["id"] in visible_ids(client, a) and personal_named(client, a, shared["name"]) == []


def test_nobody_deletes_another_users_personal_food(client, make_admin, make_user):
    _, admin = make_admin()
    _, a = make_user()
    _, b = make_user()
    personal = add_food(client, a)
    log(client, a, personal["id"])
    for headers in (admin, b):
        assert client.delete(f"/foods/{personal['id']}?keep_personal_copies=true", headers=headers).status_code == 404
    assert client.get(f"/admin/foods/{personal['id']}/usage", headers=admin).status_code == 404
    # and the owner's own used personal food is still blocked, even with the flag
    assert client.delete(f"/foods/{personal['id']}?keep_personal_copies=true", headers=a).json()["detail"]["code"] == "food_in_use"
    assert personal["id"] in visible_ids(client, a)


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

    # Renaming a personal food to a global name warns too.
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
