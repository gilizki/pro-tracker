"""Tests for friend push notifications.

No real push is ever sent: the service's sender is replaced by FakeSender, and
pywebpush.webpush itself is patched to fail the test if anything reaches it.
The server clock is set through FastAPI's dependency override of ``israel_now``.
"""
import datetime as dt
import json
import uuid
from pathlib import Path

import pytest
import pywebpush
from sqlalchemy.exc import IntegrityError

import database
import main
import notifications_push
import notifications_routes
from conftest import REAL_DB, TEST_DB
from israel_time import TIME_ZONE, israel_now, tracker_day
from notifications_config import BULLDOZER, DAILY_CAP, MIDNIGHT_MUNCH, PROTEIN_MACHINE
from notifications_models import PushSubscription, SentNotification
from notifications_repository import CAP_REACHED, DUPLICATE, RESERVED, SentNotificationRepository
from test_day_boundary import CASES, NODE, tracker_days
from test_friends import befriend

BASE_DAY = dt.date(2026, 11, 10)


def at(hour, minute=0, day=BASE_DAY):
    return dt.datetime(day.year, day.month, day.day, hour, minute, tzinfo=TIME_ZONE)


class FakeSender:
    enabled = True
    public_key = "test-public-key"

    def __init__(self):
        self.sent = []          # (endpoint, payload)
        self.result = "sent"    # or "gone" / "failed", or an Exception instance to raise
        self.results_by_endpoint = {}

    def send(self, endpoint, p256dh, auth, payload, ttl):
        result = self.results_by_endpoint.get(endpoint, self.result)
        if isinstance(result, Exception):
            raise result
        self.sent.append((endpoint, payload))
        return result

    def types_to(self, endpoint):
        return [payload["type"] for e, payload in self.sent if e == endpoint]


@pytest.fixture(autouse=True)
def fake_sender(monkeypatch):
    def real_push_forbidden(*args, **kwargs):
        raise AssertionError("A test tried to send a real push notification")
    monkeypatch.setattr(pywebpush, "webpush", real_push_forbidden)
    fake = FakeSender()
    monkeypatch.setattr(notifications_routes.notification_service, "sender", fake)
    return fake


class Clock:
    def __init__(self):
        self.moment = at(10, 0)

    def set(self, moment):
        self.moment = moment

    @property
    def day(self):
        return tracker_day(self.moment)


@pytest.fixture
def clock():
    c = Clock()
    main.app.dependency_overrides[israel_now] = lambda: c.moment
    yield c
    main.app.dependency_overrides.pop(israel_now, None)


def subscribe(client, headers):
    endpoint = f"https://fcm.googleapis.com/fcm/send/{uuid.uuid4().hex}"
    response = client.post("/notifications/subscriptions", headers=headers,
                           json={"endpoint": endpoint, "expirationTime": None,
                                 "keys": {"p256dh": "BPublicKey_-abc", "auth": "AuthSecret_-x"}})
    assert response.status_code == 200, response.text
    return endpoint


def log_meal(client, headers, calories=0.0, protein=0.0, day=None, clock=None):
    """Logs 100 g of a new food, so the meal adds exactly ``calories`` and ``protein``."""
    name = f"SecretFood_{uuid.uuid4().hex[:8]}"
    food = client.post("/foods/", json={"name": name, "calories": calories, "protein": protein}, headers=headers)
    assert food.status_code == 200, food.text
    day = day or clock.day
    response = client.post("/records/", json={"food_id": food.json()["id"], "amount_grams": 100,
                                              "date": day.isoformat()}, headers=headers)
    assert response.status_code == 200, response.text
    return name


@pytest.fixture
def pair(client, make_user):
    """A friend F (who logs meals) and a subscribed recipient R. Returns (f, f_headers, r, r_headers, endpoint)."""
    f, f_headers = make_user()
    r, r_headers = make_user()
    befriend(client, f_headers, r, r_headers)
    return f, f_headers, r, r_headers, subscribe(client, r_headers)


def sent_rows(recipient_id=None, friend_id=None):
    db = database.SessionLocal()
    try:
        query = db.query(SentNotification)
        if recipient_id is not None:
            query = query.filter(SentNotification.recipient_id == recipient_id)
        if friend_id is not None:
            query = query.filter(SentNotification.friend_id == friend_id)
        return [(row.recipient_id, row.friend_id, row.type, row.day, row.slot, row.created_at) for row in query]
    finally:
        db.close()


# ---------------------------------------------------------------- safety
def test_tests_use_temp_database_and_no_real_keys():
    used = Path(database.engine.url.database).resolve()
    assert used == TEST_DB and used != REAL_DB
    assert notifications_push.WebPushSender().enabled is False  # conftest removed any VAPID_* env vars


# ---------------------------------------------------------------- thresholds
@pytest.mark.parametrize("protein,fires", [(119, False), (120, True)])
def test_protein_machine_boundary(client, pair, clock, fake_sender, protein, fires):
    f, f_headers, r, _, endpoint = pair
    log_meal(client, f_headers, protein=protein, clock=clock)
    assert fake_sender.types_to(endpoint) == ([PROTEIN_MACHINE] if fires else [])


@pytest.mark.parametrize("calories,fires", [(1999, False), (2000, True)])
def test_bulldozer_calorie_boundary(client, pair, clock, fake_sender, calories, fires):
    _, f_headers, _, _, endpoint = pair
    log_meal(client, f_headers, calories=calories, clock=clock)
    assert fake_sender.types_to(endpoint) == ([BULLDOZER] if fires else [])


@pytest.mark.parametrize("moment,expected", [
    (at(19, 59), [BULLDOZER]),
    (at(20, 0), []),
    (at(2, 0), [BULLDOZER]),         # first minute of the tracker day
    (at(1, 59), [MIDNIGHT_MUNCH]),   # not Bulldozer: 01:59 is after 20:00 of this tracker day
])
def test_bulldozer_time_boundary(client, pair, clock, fake_sender, moment, expected):
    _, f_headers, _, _, endpoint = pair
    clock.set(moment)
    log_meal(client, f_headers, calories=2500, clock=clock)
    assert fake_sender.types_to(endpoint) == expected


@pytest.mark.parametrize("moment,fires", [(at(0, 0), True), (at(1, 59), True), (at(2, 0), False), (at(23, 59), False)])
def test_midnight_munch_boundary(client, pair, clock, fake_sender, moment, fires):
    _, f_headers, _, _, endpoint = pair
    clock.set(moment)
    log_meal(client, f_headers, calories=50, protein=1, clock=clock)
    assert fake_sender.types_to(endpoint) == ([MIDNIGHT_MUNCH] if fires else [])


# ---------------------------------------------------------------- once per day / cap / priority
def test_each_type_once_per_day_per_friend(client, pair, clock, fake_sender):
    f, f_headers, r, _, endpoint = pair
    log_meal(client, f_headers, protein=130, clock=clock)
    clock.set(at(11, 0))
    log_meal(client, f_headers, protein=10, clock=clock)   # still above 120: not sent again
    assert fake_sender.types_to(endpoint) == [PROTEIN_MACHINE]

    clock.set(at(10, 0, BASE_DAY + dt.timedelta(days=1)))  # next tracker day: allowed again
    log_meal(client, f_headers, protein=125, clock=clock)
    assert fake_sender.types_to(endpoint) == [PROTEIN_MACHINE, PROTEIN_MACHINE]

    rows = sent_rows(r["id"], f["id"])
    assert len(rows) == 2 and all(row[2] == PROTEIN_MACHINE and row[5] is not None for row in rows)
    assert {row[3] for row in rows} == {BASE_DAY, BASE_DAY + dt.timedelta(days=1)}


def test_daily_cap_across_friends_and_types(client, make_user, clock, fake_sender):
    r, r_headers = make_user()
    endpoint = subscribe(client, r_headers)
    friends = []
    for _ in range(3):
        f, f_headers = make_user()
        befriend(client, f_headers, r, r_headers)
        friends.append(f_headers)

    clock.set(at(1, 0))                              # Midnight Munch counts toward the cap too
    log_meal(client, friends[0], calories=10, clock=clock)
    clock.set(at(1, 30))
    log_meal(client, friends[1], protein=150, clock=clock)   # Midnight Munch (priority) for friend 2
    log_meal(client, friends[2], protein=150, clock=clock)   # cap reached: nothing
    log_meal(client, friends[2], protein=1, clock=clock)
    assert fake_sender.types_to(endpoint) == [MIDNIGHT_MUNCH, MIDNIGHT_MUNCH]
    assert len(sent_rows(r["id"])) == DAILY_CAP == 2
    assert sorted(row[4] for row in sent_rows(r["id"])) == [1, 2]


def test_cap_is_per_recipient(client, make_user, clock, fake_sender):
    f, f_headers = make_user()
    recipients = []
    for _ in range(3):
        r, r_headers = make_user()
        befriend(client, f_headers, r, r_headers)
        recipients.append(subscribe(client, r_headers))
    log_meal(client, f_headers, protein=200, clock=clock)
    assert all(fake_sender.types_to(endpoint) == [PROTEIN_MACHINE] for endpoint in recipients)


def test_one_notification_per_meal_by_priority(client, pair, clock, fake_sender):
    f, f_headers, r, _, endpoint = pair
    clock.set(at(1, 0))
    log_meal(client, f_headers, protein=150, clock=clock)    # Midnight Munch + Protein fire: only Midnight
    assert fake_sender.types_to(endpoint) == [MIDNIGHT_MUNCH]
    assert [row[2] for row in sent_rows(r["id"])] == [MIDNIGHT_MUNCH]  # Protein was NOT reserved
    clock.set(at(1, 30))
    log_meal(client, f_headers, protein=1, clock=clock)      # Protein still holds: sent now (cap 2 reached)
    assert fake_sender.types_to(endpoint) == [MIDNIGHT_MUNCH, PROTEIN_MACHINE]


def test_bulldozer_before_protein_machine(client, make_user, clock, fake_sender):
    f, f_headers = make_user()
    r, r_headers = make_user()
    befriend(client, f_headers, r, r_headers)
    endpoint = subscribe(client, r_headers)
    log_meal(client, f_headers, calories=2500, protein=150, clock=clock)
    assert fake_sender.types_to(endpoint) == [BULLDOZER]
    clock.set(at(21, 0))  # after 20:00 Bulldozer no longer fires, but it was already sent anyway
    log_meal(client, f_headers, calories=10, clock=clock)
    assert fake_sender.types_to(endpoint) == [BULLDOZER, PROTEIN_MACHINE]


# ---------------------------------------------------------------- which meals count
@pytest.mark.parametrize("offset_days", [-1, -7, 1])
def test_meals_for_other_days_never_trigger(client, pair, clock, fake_sender, offset_days):
    _, f_headers, r, _, endpoint = pair
    clock.set(at(1, 0))  # Midnight Munch time, with huge totals: still nothing
    log_meal(client, f_headers, calories=5000, protein=500, day=clock.day + dt.timedelta(days=offset_days))
    assert fake_sender.sent == [] and sent_rows(r["id"]) == []


def test_failed_save_sends_nothing(client, pair, clock, fake_sender):
    _, f_headers, r, _, _ = pair
    clock.set(at(1, 0))
    missing_food = client.post("/records/", json={"food_id": 99999999, "amount_grams": 100,
                                                  "date": clock.day.isoformat()}, headers=f_headers)
    assert missing_food.status_code == 404
    invalid = client.post("/records/", json={"food_id": 1, "amount_grams": -5, "date": clock.day.isoformat()},
                          headers=f_headers)
    assert invalid.status_code == 422
    assert client.post("/records/", json={"food_id": 1, "amount_grams": 5, "date": clock.day.isoformat()}).status_code == 401
    assert fake_sender.sent == [] and sent_rows(r["id"]) == []


def test_failed_push_does_not_break_meal_logging(client, pair, clock, fake_sender):
    _, f_headers, r, _, endpoint = pair
    fake_sender.result = RuntimeError("push service down")
    name = log_meal(client, f_headers, protein=150, clock=clock)  # asserts 200 inside
    meals = client.get(f"/summary/{clock.day.isoformat()}", headers=f_headers).json()["meals"]
    assert [m["food_name"] for m in meals] == [name]
    assert len(sent_rows(r["id"])) == 1  # recorded once; not retried, not duplicated


def test_crash_in_notification_code_does_not_break_meal_logging(client, pair, clock, monkeypatch):
    _, f_headers, _, _, _ = pair

    def crash(*args, **kwargs):
        raise RuntimeError("bug")
    monkeypatch.setattr(notifications_routes.notification_service, "handle_meal", crash)
    log_meal(client, f_headers, protein=150, clock=clock)  # asserts 200 inside


# ---------------------------------------------------------------- who receives
def test_non_friends_pending_and_removed_friends_get_nothing(client, make_user, clock, fake_sender):
    f, f_headers = make_user()
    stranger, stranger_headers = make_user()
    pending, pending_headers = make_user()
    removed, removed_headers = make_user()
    endpoints = [subscribe(client, h) for h in (stranger_headers, pending_headers, removed_headers)]
    client.post("/friends/requests", json={"username": pending["username"]}, headers=f_headers)
    befriend(client, f_headers, removed, removed_headers)
    assert client.delete(f"/friends/{f['id']}", headers=removed_headers).status_code == 200

    log_meal(client, f_headers, protein=200, calories=3000, clock=clock)
    assert fake_sender.sent == []
    assert all(sent_rows(u["id"]) == [] for u in (stranger, pending, removed))
    assert not any(fake_sender.types_to(e) for e in endpoints)


def test_the_friend_does_not_notify_themself(client, pair, clock, fake_sender):
    f, f_headers, _, _, recipient_endpoint = pair
    own_endpoint = subscribe(client, f_headers)
    log_meal(client, f_headers, protein=200, clock=clock)
    assert fake_sender.types_to(own_endpoint) == [] and fake_sender.types_to(recipient_endpoint) == [PROTEIN_MACHINE]


def test_recipient_without_device_does_not_use_up_cap(client, make_user, clock, fake_sender):
    f, f_headers = make_user()
    r, r_headers = make_user()
    befriend(client, f_headers, r, r_headers)
    log_meal(client, f_headers, protein=150, clock=clock)
    assert sent_rows(r["id"]) == []
    endpoint = subscribe(client, r_headers)
    log_meal(client, f_headers, protein=1, clock=clock)
    assert fake_sender.types_to(endpoint) == [PROTEIN_MACHINE]


def test_every_device_gets_it_and_it_counts_once(client, pair, clock, fake_sender):
    _, f_headers, r, r_headers, first = pair
    second = subscribe(client, r_headers)
    log_meal(client, f_headers, protein=150, clock=clock)
    assert fake_sender.types_to(first) == [PROTEIN_MACHINE] and fake_sender.types_to(second) == [PROTEIN_MACHINE]
    assert len(sent_rows(r["id"])) == 1


def test_expired_subscriptions_are_removed(client, pair, clock, fake_sender):
    _, f_headers, r, r_headers, expired = pair
    working = subscribe(client, r_headers)
    fake_sender.results_by_endpoint[expired] = "gone"
    log_meal(client, f_headers, protein=150, clock=clock)
    db = database.SessionLocal()
    try:
        endpoints = {s.endpoint for s in db.query(PushSubscription).filter(PushSubscription.user_id == r["id"])}
    finally:
        db.close()
    assert endpoints == {working}


def test_failed_but_not_gone_subscription_is_kept(client, pair, clock, fake_sender):
    _, f_headers, r, _, endpoint = pair
    fake_sender.result = "failed"
    log_meal(client, f_headers, protein=150, clock=clock)
    db = database.SessionLocal()
    try:
        assert db.query(PushSubscription).filter(PushSubscription.endpoint == endpoint).count() == 1
    finally:
        db.close()


def test_push_disabled_without_keys_sends_and_records_nothing(client, pair, clock, monkeypatch):
    _, f_headers, r, _, _ = pair
    monkeypatch.setattr(notifications_routes.notification_service, "sender", notifications_push.WebPushSender())
    assert client.get("/notifications/config").json() == {"public_key": None}
    log_meal(client, f_headers, protein=150, clock=clock)
    assert sent_rows(r["id"]) == []


# ---------------------------------------------------------------- privacy & payload
def test_payload_reveals_only_the_type(client, pair, clock, fake_sender):
    f, f_headers, _, _, endpoint = pair
    food_name = log_meal(client, f_headers, calories=2345, protein=137, clock=clock)
    [(sent_to, payload)] = fake_sender.sent
    text = json.dumps(payload, ensure_ascii=False)
    assert sent_to == endpoint and f["username"] in payload["body"]
    assert food_name not in text and "137" not in text and "2345" not in text
    assert set(payload) == {"type", "title", "body", "url", "silent", "tag"}
    assert payload["type"] == BULLDOZER and payload["url"] == "/?open=friends" and payload["silent"] is False


def test_midnight_munch_is_silent_and_opens_main_screen(client, pair, clock, fake_sender):
    _, f_headers, _, _, _ = pair
    clock.set(at(0, 30))
    log_meal(client, f_headers, calories=300, clock=clock)
    [(_, payload)] = fake_sender.sent
    assert payload["type"] == MIDNIGHT_MUNCH and payload["silent"] is True and payload["url"] == "/"


def test_protein_machine_opens_friends(client, pair, clock, fake_sender):
    _, f_headers, _, _, _ = pair
    log_meal(client, f_headers, protein=150, clock=clock)
    assert fake_sender.sent[0][1]["url"] == "/?open=friends"


# ---------------------------------------------------------------- subscriptions API
@pytest.mark.parametrize("endpoint", [
    "http://fcm.googleapis.com/fcm/send/x",            # not https
    "https://example.com/push",                        # not a push service
    "https://fcm.googleapis.com.evil.example/x",       # look-alike host
    "https://user:pw@fcm.googleapis.com/x",
    "https://fcm.googleapis.com:8443/x",
    "https://127.0.0.1/x",
])
def test_subscription_rejects_unknown_endpoints(client, make_user, endpoint):
    _, headers = make_user()
    response = client.post("/notifications/subscriptions", headers=headers,
                           json={"endpoint": endpoint, "keys": {"p256dh": "abc", "auth": "def"}})
    assert response.status_code == 422


@pytest.mark.parametrize("endpoint", ["https://fcm.googleapis.com/fcm/send/abc", "https://web.push.apple.com/QK1",
                                      "https://updates.push.services.mozilla.com/wpush/v2/x",
                                      "https://wns2-db5p.notify.windows.com/w/?token=x"])
def test_subscription_accepts_real_push_services(client, make_user, endpoint):
    _, headers = make_user()
    response = client.post("/notifications/subscriptions", headers=headers,
                           json={"endpoint": endpoint + uuid.uuid4().hex, "keys": {"p256dh": "abc", "auth": "def"}})
    assert response.status_code == 200, response.text


def test_subscribe_requires_login_and_moves_device_to_new_user(client, make_user):
    a, a_headers = make_user()
    b, b_headers = make_user()
    body = {"endpoint": f"https://fcm.googleapis.com/fcm/send/{uuid.uuid4().hex}", "keys": {"p256dh": "k1", "auth": "a1"}}
    assert client.post("/notifications/subscriptions", json=body).status_code == 401
    assert client.post("/notifications/subscriptions", json=body, headers=a_headers).status_code == 200
    assert client.post("/notifications/subscriptions", json=body, headers=a_headers).status_code == 200  # idempotent
    assert client.post("/notifications/subscriptions", json=body, headers=b_headers).status_code == 200
    db = database.SessionLocal()
    try:
        rows = db.query(PushSubscription).filter(PushSubscription.endpoint == body["endpoint"]).all()
        assert len(rows) == 1 and rows[0].user_id == b["id"]
    finally:
        db.close()


def test_unsubscribe_on_logout_needs_no_login(client, make_user):
    _, headers = make_user()
    endpoint = subscribe(client, headers)
    assert client.post("/notifications/unsubscribe", json={"endpoint": endpoint}).json() == {"status": "unsubscribed"}
    db = database.SessionLocal()
    try:
        assert db.query(PushSubscription).filter(PushSubscription.endpoint == endpoint).count() == 0
    finally:
        db.close()


# ---------------------------------------------------------------- database constraints
def test_reserve_is_once_per_day_and_capped(make_user):
    users = [make_user()[0]["id"] for _ in range(4)]
    recipient, friends = users[0], users[1:]
    repo = SentNotificationRepository()
    db = database.SessionLocal()
    try:
        assert repo.reserve(db, recipient, friends[0], PROTEIN_MACHINE, BASE_DAY, 2) == RESERVED
        assert repo.reserve(db, recipient, friends[0], PROTEIN_MACHINE, BASE_DAY, 2) == DUPLICATE
        assert repo.reserve(db, recipient, friends[1], BULLDOZER, BASE_DAY, 2) == RESERVED
        assert repo.reserve(db, recipient, friends[2], MIDNIGHT_MUNCH, BASE_DAY, 2) == CAP_REACHED
    finally:
        db.close()


def test_concurrent_reservations_respect_cap_and_once_per_day(make_user):
    """Simultaneous meals: many threads race to reserve; the constraints decide."""
    from concurrent.futures import ThreadPoolExecutor

    recipient = make_user()[0]["id"]
    friends = [make_user()[0]["id"] for _ in range(4)]
    repo = SentNotificationRepository()

    def reserve(friend_id):
        db = database.SessionLocal()
        try:
            return repo.reserve(db, recipient, friend_id, PROTEIN_MACHINE, BASE_DAY, DAILY_CAP)
        finally:
            db.close()

    jobs = [friends[0]] * 4 + friends[1:] * 2   # same friend 4x at once, others twice each
    with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
        outcomes = list(pool.map(reserve, jobs))
    assert outcomes.count(RESERVED) == DAILY_CAP
    rows = sent_rows(recipient)
    assert len(rows) == DAILY_CAP and len({row[1] for row in rows}) == DAILY_CAP


@pytest.mark.parametrize("second", [
    {"type": PROTEIN_MACHINE, "slot": 2},   # same recipient/friend/type/day, other slot
    {"type": BULLDOZER, "slot": 1},         # same recipient/day/slot, other type
])
def test_database_constraints_block_duplicates(make_user, second):
    recipient, friend = make_user()[0]["id"], make_user()[0]["id"]
    db = database.SessionLocal()
    try:
        db.add(SentNotification(recipient_id=recipient, friend_id=friend, type=PROTEIN_MACHINE, day=BASE_DAY, slot=1))
        db.commit()
        db.add(SentNotification(recipient_id=recipient, friend_id=friend, day=BASE_DAY, **second))
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()
    finally:
        db.close()


# ---------------------------------------------------------------- the real sender, without network
class _Response:
    def __init__(self, status_code):
        self.status_code = status_code


@pytest.fixture
def vapid_env(monkeypatch):
    monkeypatch.setenv("VAPID_PUBLIC_KEY", "pub")
    monkeypatch.setenv("VAPID_PRIVATE_KEY", "priv")
    monkeypatch.setenv("VAPID_SUBJECT", "https://pro-tracker-rkcu.onrender.com")


@pytest.mark.parametrize("status,expected", [(410, "gone"), (404, "gone"), (500, "failed"), (429, "failed")])
def test_web_push_sender_maps_errors(monkeypatch, vapid_env, status, expected):
    def fake_webpush(**kwargs):
        raise pywebpush.WebPushException("rejected", response=_Response(status))
    monkeypatch.setattr(pywebpush, "webpush", fake_webpush)
    assert notifications_push.WebPushSender().send("https://fcm.googleapis.com/x", "k", "a", {"t": 1}, 60) == expected


def test_web_push_sender_success_and_network_error(monkeypatch, vapid_env):
    calls = []

    def fake_webpush(**kwargs):
        calls.append(kwargs)
    monkeypatch.setattr(pywebpush, "webpush", fake_webpush)
    sender = notifications_push.WebPushSender()
    assert sender.enabled and sender.public_key == "pub"
    assert sender.send("https://fcm.googleapis.com/x", "k", "a", {"body": "שלום"}, 60) == "sent"
    sender.send("https://web.push.apple.com/y", "k", "a", {}, 60)
    assert calls[0]["vapid_claims"] is not calls[1]["vapid_claims"]  # fresh claims per push
    assert calls[0]["ttl"] == 60 and json.loads(calls[0]["data"]) == {"body": "שלום"}

    def broken(**kwargs):
        raise ConnectionError("no network")
    monkeypatch.setattr(pywebpush, "webpush", broken)
    assert sender.send("https://fcm.googleapis.com/x", "k", "a", {}, 60) == "failed"


# ---------------------------------------------------------------- Israel time: server agrees with day.js
@pytest.mark.parametrize("name,instant,expected", CASES, ids=[c[0] for c in CASES])
def test_server_tracker_day_matches_day_js_cases(name, instant, expected):
    moment = dt.datetime.fromisoformat(instant.replace("Z", "+00:00"))
    assert tracker_day(moment).isoformat() == expected


@pytest.mark.skipif(NODE is None, reason="Node.js is not installed")
def test_server_tracker_day_matches_day_js_every_15_minutes_around_dst():
    instants = []
    for start in ("2026-03-25T00:00:00Z", "2026-10-23T00:00:00Z", "2026-11-04T00:00:00Z"):
        first = dt.datetime.fromisoformat(start.replace("Z", "+00:00"))
        instants += [first + dt.timedelta(minutes=15 * i) for i in range(4 * 24 * 4)]  # 4 days
    js_days = tracker_days([i.strftime("%Y-%m-%dT%H:%M:%SZ") for i in instants])
    assert js_days == [tracker_day(i).isoformat() for i in instants]


def test_israel_now_is_israel_time():
    now = israel_now()
    assert now.tzinfo is TIME_ZONE
    assert abs((now - dt.datetime.now(dt.timezone.utc)).total_seconds()) < 5


# ---------------------------------------------------------------- app files
def test_service_worker_manifest_and_script_are_served(client):
    sw = client.get("/sw.js")
    assert sw.status_code == 200 and "javascript" in sw.headers["content-type"] and "notificationclick" in sw.text
    assert "addEventListener('fetch'" not in sw.text  # no fetch handler: the app loads as before
    manifest = client.get("/manifest.webmanifest")
    assert manifest.status_code == 200 and manifest.headers["content-type"].startswith("application/manifest+json")
    assert manifest.json()["start_url"] == "/" and manifest.json()["display"] == "standalone"
    for icon in manifest.json()["icons"]:
        assert client.get(icon["src"]).status_code == 200
    page = client.get("/").text
    assert '/manifest.webmanifest' in page and '/frontend/notifications.js' in page
    assert "הפעל התראות" in client.get("/frontend/notifications.js").text
