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

    # A global food used by any user cannot be deleted, even by an admin.
    shared = add_food(client, admin, is_global=True)
    log(client, b, shared["id"])
    assert client.delete(f"/foods/{shared['id']}", headers=admin).status_code == 409
    assert shared["id"] in visible_ids(client, b)


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
