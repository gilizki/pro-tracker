"""Tests for friend push notifications.

No real push is ever sent: the service's sender is replaced by FakeSender, and
pywebpush.webpush itself is patched to fail the test if anything reaches it.
The server clock is set through FastAPI's dependency override of ``israel_now``.
"""
import datetime as dt
import json
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
import pywebpush
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.core import database
import main
from app.notifications import push as notifications_push
from app.notifications import routes as notifications_routes
from conftest import REAL_DB, TEST_DB
from app.core.israel_time import TIME_ZONE, israel_now, tracker_day
from app.notifications.config import (
    ALL_TYPES, BEFORE_TEN, BULLDOZER, EARLY_BIRD, MEAL_PROTEIN, MESSAGES, MIDNIGHT_MUNCH, PRIORITY, PROTEIN_MACHINE,
)
from app.notifications.models import PushSubscription, SentNotification
from app.notifications.repository import SentNotificationRepository
from app.notifications.service import build_payload, fired_types, format_grams
from test_day_boundary import CASES, NODE, tracker_days
from test_friends import befriend

BASE_DAY = dt.date(2026, 11, 10)
NEXT_DAY = BASE_DAY + dt.timedelta(days=1)


def at(hour, minute=0, day=BASE_DAY):
    return dt.datetime(day.year, day.month, day.day, hour, minute, tzinfo=TIME_ZONE)


class FakeSender:
    enabled = True
    public_key = "test-public-key"

    def __init__(self):
        self.sent = []          # (endpoint, payload)
        self.result = "sent"    # or "gone" / "failed", or an Exception instance to raise
        self.results_by_endpoint = {}
        self._lock = threading.Lock()

    def send(self, endpoint, p256dh, auth, payload, ttl):
        result = self.results_by_endpoint.get(endpoint, self.result)
        if isinstance(result, Exception):
            raise result
        with self._lock:
            self.sent.append((endpoint, payload))
        return result

    def types_to(self, endpoint):
        return [payload["type"] for e, payload in self.sent if e == endpoint]

    def payloads_to(self, endpoint):
        return [payload for e, payload in self.sent if e == endpoint]


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
        self.moment = at(12, 0)  # outside every time window

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


def log_meal(client, headers, calories=0.0, protein=0.0, day=None, clock=None, grams=100):
    """Logs ``grams`` of a new food whose per-100 g values are ``calories`` and ``protein``.
    With the default 100 g, the meal adds exactly ``calories`` and ``protein``. Returns the record id."""
    name = f"SecretFood_{uuid.uuid4().hex[:8]}"
    food = client.post("/foods/", json={"name": name, "calories": calories, "protein": protein}, headers=headers)
    assert food.status_code == 200, food.text
    day = day or clock.day
    response = client.post("/records/", json={"food_id": food.json()["id"], "amount_grams": grams,
                                              "date": day.isoformat()}, headers=headers)
    assert response.status_code == 200, response.text
    log_meal.last_food_name = name
    return response.json()["id"]


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
        query = db.query(SentNotification).order_by(SentNotification.id)
        if recipient_id is not None:
            query = query.filter(SentNotification.recipient_id == recipient_id)
        if friend_id is not None:
            query = query.filter(SentNotification.friend_id == friend_id)
        return [(row.recipient_id, row.friend_id, row.type, row.day, row.created_at) for row in query]
    finally:
        db.close()


# ---------------------------------------------------------------- safety
def test_tests_use_temp_database_and_no_real_keys():
    used = Path(database.engine.url.database).resolve()
    assert used == TEST_DB and used != REAL_DB
    assert notifications_push.WebPushSender().enabled is False  # conftest removed any VAPID_* env vars


# ---------------------------------------------------------------- configuration
def test_there_are_six_types_in_one_priority_order():
    assert PRIORITY == (MIDNIGHT_MUNCH, EARLY_BIRD, BEFORE_TEN, MEAL_PROTEIN, BULLDOZER, PROTEIN_MACHINE)
    assert len(set(ALL_TYPES)) == 6 and set(MESSAGES) == set(ALL_TYPES)
    assert all(len(t) <= 32 for t in ALL_TYPES)  # fits sent_notifications.type


@pytest.mark.parametrize("notification_type,title,body", [
    (EARLY_BIRD, "☀️ השכמה מוקדמת", "Dana כבר אוכל. הוא ישן בכלל?"),
    (BEFORE_TEN, "🍳 ארוחת בוקר מוקדמת", "עוד לא עשר בבוקר וDana כבר דפק ארוחה"),
    (MEAL_PROTEIN, "💨 פצצת חלבון", "Dana אכל 32.5 גרם חלבון. עם כזו כמות הוא בטח מפליץ ניחוחות כמו חירייה"),
])
def test_new_texts_titles_and_behaviour(notification_type, title, body):
    payload = build_payload(notification_type, 7, "Dana", 32.5)
    assert payload == {"type": notification_type, "title": title, "body": body,
                       "url": "/?open=friends", "silent": False, "tag": f"{notification_type}-7"}


@pytest.mark.parametrize("grams,shown", [
    (25.1, "25.1"), (32.5, "32.5"), (30.0, "30"), (26, "26"), (25.04, "25"), (25.05, "25.1"),
    (100 * 25.1 / 100, "25.1"), (125 * 20.08 / 100, "25.1"), (150 * 21.7 / 100, "32.6"),
])
def test_protein_is_shown_with_one_decimal(grams, shown):
    assert format_grams(grams) == shown


# ---------------------------------------------------------------- which types fire (pure rules)
@pytest.mark.parametrize("moment,expected", [
    (at(0, 0), [MIDNIGHT_MUNCH]),
    (at(1, 59), [MIDNIGHT_MUNCH]),               # previous tracker day: no morning types
    (at(2, 0), [EARLY_BIRD, BEFORE_TEN]),        # first minute of the tracker day
    (at(7, 59), [EARLY_BIRD, BEFORE_TEN]),
    (at(8, 0), [BEFORE_TEN]),
    (at(9, 59), [BEFORE_TEN]),
    (at(10, 0), []),
    (at(23, 59), []),
])
def test_time_windows(moment, expected):
    assert fired_types(0, 0, moment) == expected


def test_all_six_can_fire_together_in_priority_order():
    assert fired_types(200, 3000, at(7, 0), meal_protein=30) == [EARLY_BIRD, BEFORE_TEN, MEAL_PROTEIN,
                                                                 BULLDOZER, PROTEIN_MACHINE]
    assert fired_types(200, 3000, at(1, 0), meal_protein=30) == [MIDNIGHT_MUNCH, MEAL_PROTEIN, PROTEIN_MACHINE]


@pytest.mark.parametrize("meal_protein,fires", [(None, False), (0, False), (25, False), (25.0001, True), (25.1, True)])
def test_meal_protein_rule(meal_protein, fires):
    assert (MEAL_PROTEIN in fired_types(0, 0, at(12, 0), meal_protein)) is fires


# ---------------------------------------------------------------- thresholds through the API
@pytest.mark.parametrize("moment,expected", [
    (at(7, 59), EARLY_BIRD), (at(8, 0), BEFORE_TEN), (at(9, 59), BEFORE_TEN), (at(10, 0), None),
    (at(2, 0), EARLY_BIRD), (at(1, 59), MIDNIGHT_MUNCH),
])
def test_morning_boundaries(client, pair, clock, fake_sender, moment, expected):
    _, f_headers, _, _, endpoint = pair
    clock.set(moment)
    log_meal(client, f_headers, calories=50, protein=1, clock=clock)
    assert fake_sender.types_to(endpoint) == ([expected] if expected else [])


@pytest.mark.parametrize("protein_per_100g,grams,expected_text", [
    (25, 100, None),          # exactly 25 g: not more than 25
    (25.1, 100, "25.1"),
    (12.6, 200, "25.2"),      # per-100 g value is low, the meal itself is above 25
    (60, 50, "30"),
    (30, 50, None),           # per-100 g value is high, the meal itself is only 15 g
    (0, 1000, None),          # a food without protein never fires
])
def test_meal_protein_boundary_uses_grams_eaten(client, pair, clock, fake_sender, protein_per_100g, grams, expected_text):
    f, f_headers, _, _, endpoint = pair
    log_meal(client, f_headers, protein=protein_per_100g, grams=grams, clock=clock)
    payloads = fake_sender.payloads_to(endpoint)
    if expected_text is None:
        assert payloads == []
    else:
        [payload] = payloads
        assert payload["type"] == MEAL_PROTEIN
        assert payload["body"] == f"{f['username']} אכל {expected_text} גרם חלבון. עם כזו כמות הוא בטח מפליץ ניחוחות כמו חירייה"


@pytest.mark.parametrize("second_meal,fires", [(19, False), (20, True)])
def test_protein_machine_boundary(client, pair, clock, fake_sender, second_meal, fires):
    _, f_headers, _, _, endpoint = pair
    log_meal(client, f_headers, protein=100, clock=clock)          # 100 g in one meal: meal protein
    log_meal(client, f_headers, protein=second_meal, clock=clock)  # day total 119 / 120
    assert fake_sender.types_to(endpoint) == [MEAL_PROTEIN] + ([PROTEIN_MACHINE] if fires else [])


@pytest.mark.parametrize("calories,fires", [(1999, False), (2000, True)])
def test_bulldozer_calorie_boundary(client, pair, clock, fake_sender, calories, fires):
    _, f_headers, _, _, endpoint = pair
    log_meal(client, f_headers, calories=calories, clock=clock)
    assert fake_sender.types_to(endpoint) == ([BULLDOZER] if fires else [])


@pytest.mark.parametrize("moment,expected", [
    (at(19, 59), [BULLDOZER]),
    (at(20, 0), []),
    (at(10, 0), [BULLDOZER]),
    (at(2, 0), [EARLY_BIRD]),        # Bulldozer applies too, but early_bird comes first
    (at(1, 59), [MIDNIGHT_MUNCH]),   # not Bulldozer: 01:59 is after 20:00 of this tracker day
])
def test_bulldozer_time_boundary(client, pair, clock, fake_sender, moment, expected):
    _, f_headers, _, _, endpoint = pair
    clock.set(moment)
    log_meal(client, f_headers, calories=2500, clock=clock)
    assert fake_sender.types_to(endpoint) == expected


@pytest.mark.parametrize("moment,expected", [(at(0, 0), [MIDNIGHT_MUNCH]), (at(1, 59), [MIDNIGHT_MUNCH]),
                                             (at(2, 0), [EARLY_BIRD]), (at(23, 59), [])])
def test_midnight_munch_boundary(client, pair, clock, fake_sender, moment, expected):
    _, f_headers, _, _, endpoint = pair
    clock.set(moment)
    log_meal(client, f_headers, calories=50, protein=1, clock=clock)
    assert fake_sender.types_to(endpoint) == expected


# ---------------------------------------------------------------- priority / once per type per day
def test_priority_order_one_per_meal_then_nothing(client, pair, clock, fake_sender):
    """Every meal matches every remaining type: one is sent per meal, in priority order."""
    _, f_headers, r, _, endpoint = pair
    clock.set(at(7, 0))
    log_meal(client, f_headers, calories=2500, protein=150, clock=clock)
    assert [row[2] for row in sent_rows(r["id"])] == [EARLY_BIRD]   # the others were NOT marked as sent
    for minute in range(1, 6):
        clock.set(at(7, minute))
        log_meal(client, f_headers, protein=30, clock=clock)
    assert fake_sender.types_to(endpoint) == [EARLY_BIRD, BEFORE_TEN, MEAL_PROTEIN, BULLDOZER, PROTEIN_MACHINE]
    # The 5th extra meal still matched types, but all of them were already sent today: nothing.
    assert len(sent_rows(r["id"])) == 5


def test_midnight_munch_comes_first(client, pair, clock, fake_sender):
    _, f_headers, _, _, endpoint = pair
    clock.set(at(1, 0))
    for _ in range(4):
        log_meal(client, f_headers, protein=150, clock=clock)
    assert fake_sender.types_to(endpoint) == [MIDNIGHT_MUNCH, MEAL_PROTEIN, PROTEIN_MACHINE]


def test_type_already_sent_today_is_skipped(client, make_user, clock, fake_sender):
    r, r_headers = make_user()
    endpoint = subscribe(client, r_headers)
    a, a_headers = make_user()
    b, b_headers = make_user()
    befriend(client, a_headers, r, r_headers)
    befriend(client, b_headers, r, r_headers)
    clock.set(at(7, 0))
    log_meal(client, a_headers, protein=1, clock=clock)
    clock.set(at(7, 30))
    log_meal(client, b_headers, protein=1, clock=clock)      # early_bird already sent (by A): before_ten
    assert fake_sender.types_to(endpoint) == [EARLY_BIRD, BEFORE_TEN]
    assert [(row[1], row[2]) for row in sent_rows(r["id"])] == [(a["id"], EARLY_BIRD), (b["id"], BEFORE_TEN)]


def test_same_type_from_another_friend_is_not_sent_twice(client, make_user, clock, fake_sender):
    r, r_headers = make_user()
    endpoint = subscribe(client, r_headers)
    friends = []
    for _ in range(3):
        f, f_headers = make_user()
        befriend(client, f_headers, r, r_headers)
        friends.append(f_headers)
    for f_headers in friends:
        log_meal(client, f_headers, protein=10, calories=2500, clock=clock)   # Bulldozer only
    assert fake_sender.types_to(endpoint) == [BULLDOZER]
    assert len(sent_rows(r["id"])) == 1


def test_nothing_sent_when_all_applicable_types_were_sent(client, pair, clock, fake_sender):
    _, f_headers, _, _, endpoint = pair
    clock.set(at(9, 0))
    log_meal(client, f_headers, protein=1, clock=clock)      # before_ten
    log_meal(client, f_headers, protein=1, clock=clock)      # before_ten again: already sent, nothing else applies
    clock.set(at(12, 0))
    log_meal(client, f_headers, protein=1, clock=clock)      # nothing applies at all: no generic fallback
    assert fake_sender.types_to(endpoint) == [BEFORE_TEN]


def test_tracker_day_reset_follows_the_0200_rule(client, pair, clock, fake_sender):
    _, f_headers, r, _, endpoint = pair
    clock.set(at(12, 0))
    log_meal(client, f_headers, protein=20, calories=2500, clock=clock)   # Nov 10: Bulldozer
    clock.set(at(0, 30, NEXT_DAY))                                         # still tracker day Nov 10
    log_meal(client, f_headers, protein=1, clock=clock)                    # Midnight Munch
    clock.set(at(1, 59, NEXT_DAY))
    log_meal(client, f_headers, protein=1, clock=clock)                    # Midnight Munch again: nothing
    clock.set(at(2, 0, NEXT_DAY))                                          # tracker day Nov 11 begins
    log_meal(client, f_headers, protein=1, calories=2500, clock=clock)     # early_bird
    clock.set(at(12, 0, NEXT_DAY))
    log_meal(client, f_headers, protein=1, clock=clock)                    # Bulldozer again, new day
    assert fake_sender.types_to(endpoint) == [BULLDOZER, MIDNIGHT_MUNCH, EARLY_BIRD, BULLDOZER]
    assert [(row[2], row[3]) for row in sent_rows(r["id"])] == [
        (BULLDOZER, BASE_DAY), (MIDNIGHT_MUNCH, BASE_DAY), (EARLY_BIRD, NEXT_DAY), (BULLDOZER, NEXT_DAY)]


def test_each_recipient_is_tracked_separately(client, make_user, clock, fake_sender):
    f, f_headers = make_user()
    recipients = []
    for _ in range(3):
        r, r_headers = make_user()
        befriend(client, f_headers, r, r_headers)
        recipients.append(subscribe(client, r_headers))
    log_meal(client, f_headers, protein=200, clock=clock)
    assert all(fake_sender.types_to(endpoint) == [MEAL_PROTEIN] for endpoint in recipients)


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


def test_meal_deleted_before_the_task_runs_still_gets_other_types(client, pair, clock, fake_sender):
    f, f_headers, _, _, endpoint = pair
    clock.set(at(12, 0, BASE_DAY + dt.timedelta(days=30)))
    record_id = log_meal(client, f_headers, protein=200, day=BASE_DAY)   # another day: nothing sent now
    assert client.delete(f"/records/{record_id}", headers=f_headers).status_code == 200
    db = database.SessionLocal()
    try:
        notifications_routes.notification_service.handle_meal(db, f["id"], BASE_DAY, at(7, 0), record_id)
    finally:
        db.close()
    assert fake_sender.types_to(endpoint) == [EARLY_BIRD]


# ---------------------------------------------------------------- failures never affect saving a meal
def test_failed_push_does_not_break_meal_logging(client, pair, clock, fake_sender):
    _, f_headers, r, _, endpoint = pair
    fake_sender.result = RuntimeError("push service down")
    log_meal(client, f_headers, protein=150, clock=clock)  # asserts 200 inside
    meals = client.get(f"/summary/{clock.day.isoformat()}", headers=f_headers).json()["meals"]
    assert [m["food_name"] for m in meals] == [log_meal.last_food_name]
    assert len(sent_rows(r["id"])) == 1  # recorded once; not retried, not duplicated


def test_crash_in_notification_code_does_not_break_meal_logging(client, pair, clock, monkeypatch):
    _, f_headers, _, _, _ = pair

    def crash(*args, **kwargs):
        raise RuntimeError("bug")
    monkeypatch.setattr(notifications_routes.notification_service, "handle_meal", crash)
    log_meal(client, f_headers, protein=150, clock=clock)  # asserts 200 inside


def test_schema_mismatch_during_deploy_does_not_break_meal_logging(client, pair, clock, fake_sender, monkeypatch,
                                                                   caplog):
    """Deploy gap: the migration already ran, the OLD code still writes the removed ``slot``
    column. Simulated with the old code's exact INSERT against the migrated table: the
    database error stays inside the background task; the meal is saved, the response is
    200 and later requests work. (TestClient re-raises any exception that escapes a
    background task, so a 200 here proves it was swallowed.)"""
    f, f_headers, r, _, endpoint = pair

    def old_code_reserve(db, recipient_id, friend_id, notification_type, day, *args):
        db.execute(text("INSERT INTO sent_notifications (recipient_id, friend_id, type, day, slot, created_at) "
                        "VALUES (:r, :f, :t, :d, 1, CURRENT_TIMESTAMP)"),
                   {"r": recipient_id, "f": friend_id, "t": notification_type, "d": day})
        db.commit()

    monkeypatch.setattr(notifications_routes.notification_service.sent, "reserve", old_code_reserve)
    log_meal(client, f_headers, protein=150, clock=clock)  # asserts 200 inside
    meals = client.get(f"/summary/{clock.day.isoformat()}", headers=f_headers).json()["meals"]
    assert [m["food_name"] for m in meals] == [log_meal.last_food_name]
    assert fake_sender.sent == [] and sent_rows(r["id"]) == []
    assert "Notification for one recipient failed" in caplog.text and "slot" in caplog.text  # the real DB error
    log_meal(client, f_headers, protein=1, clock=clock)    # the next meal is saved as well


def test_error_escaping_the_background_task_is_swallowed(monkeypatch):
    """Even an error outside the per-recipient guard (e.g. opening the session) never escapes."""
    def broken_session():
        raise RuntimeError("database unavailable")
    monkeypatch.setattr(database, "SessionLocal", broken_session)
    notifications_routes.run_meal_notifications(1, BASE_DAY, at(12, 0), 1)  # does not raise


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

    clock.set(at(7, 0))
    log_meal(client, f_headers, protein=200, calories=3000, clock=clock)
    assert fake_sender.sent == []
    assert all(sent_rows(u["id"]) == [] for u in (stranger, pending, removed))
    assert not any(fake_sender.types_to(e) for e in endpoints)


def test_the_friend_does_not_notify_themself(client, pair, clock, fake_sender):
    f, f_headers, _, _, recipient_endpoint = pair
    own_endpoint = subscribe(client, f_headers)
    log_meal(client, f_headers, protein=200, clock=clock)
    assert fake_sender.types_to(own_endpoint) == [] and fake_sender.types_to(recipient_endpoint) == [MEAL_PROTEIN]


def test_recipient_without_device_is_not_marked_as_sent(client, make_user, clock, fake_sender):
    f, f_headers = make_user()
    r, r_headers = make_user()
    befriend(client, f_headers, r, r_headers)
    log_meal(client, f_headers, protein=150, clock=clock)
    assert sent_rows(r["id"]) == []
    endpoint = subscribe(client, r_headers)
    log_meal(client, f_headers, protein=30, clock=clock)
    assert fake_sender.types_to(endpoint) == [MEAL_PROTEIN]


def test_every_device_gets_it_and_it_counts_once(client, pair, clock, fake_sender):
    _, f_headers, r, r_headers, first = pair
    second = subscribe(client, r_headers)
    log_meal(client, f_headers, protein=150, clock=clock)
    assert fake_sender.types_to(first) == [MEAL_PROTEIN] and fake_sender.types_to(second) == [MEAL_PROTEIN]
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


# ---------------------------------------------------------------- what a payload shows
PAYLOAD_FIELDS = {"type", "title", "body", "url", "silent", "tag"}


@pytest.mark.parametrize("notification_type", ALL_TYPES)
def test_every_type_has_the_same_payload_fields_and_names_the_friend(notification_type):
    payload = build_payload(notification_type, 7, "Dana", 32.5)
    assert set(payload) == PAYLOAD_FIELDS and "Dana" in payload["body"]
    assert ("32.5" in payload["body"]) is (notification_type == MEAL_PROTEIN)  # only that type shows an amount
    assert "{" not in payload["body"] and "{" not in payload["title"]


def test_day_total_payload_shows_name_but_not_food_or_totals(client, pair, clock, fake_sender):
    """Day-total types show the friend's name and the fixed threshold, never the food or exact totals."""
    f, f_headers, _, _, endpoint = pair
    log_meal(client, f_headers, calories=2345, protein=13.7, clock=clock)
    [(sent_to, payload)] = fake_sender.sent
    text_sent = json.dumps(payload, ensure_ascii=False)
    assert sent_to == endpoint and f["username"] in payload["body"]
    # Numbers are checked in the visible text only (the tag holds the friend's id), without the username:
    # it is random hex and may itself contain digits like "2345".
    shown = (payload["title"] + payload["body"]).replace(f["username"], "")
    assert log_meal.last_food_name not in text_sent and "2345" not in shown and "13.7" not in shown
    assert set(payload) == PAYLOAD_FIELDS
    assert payload["type"] == BULLDOZER and payload["url"] == "/?open=friends" and payload["silent"] is False


def test_meal_protein_payload_shows_name_and_amount_but_not_food(client, pair, clock, fake_sender):
    f, f_headers, _, _, _ = pair
    log_meal(client, f_headers, protein=21.7, grams=150, clock=clock)   # 32.55 g
    [(_, payload)] = fake_sender.sent
    text_sent = json.dumps(payload, ensure_ascii=False)
    assert set(payload) == PAYLOAD_FIELDS and payload["type"] == MEAL_PROTEIN
    assert f["username"] in payload["body"] and "32.6" in payload["body"]
    shown = (payload["title"] + payload["body"]).replace(f["username"], "")  # random hex username may contain "150"
    assert log_meal.last_food_name not in text_sent and "21.7" not in shown and "150" not in shown


def test_midnight_munch_is_silent_and_opens_main_screen(client, pair, clock, fake_sender):
    _, f_headers, _, _, _ = pair
    clock.set(at(0, 30))
    log_meal(client, f_headers, calories=300, clock=clock)
    [(_, payload)] = fake_sender.sent
    assert payload["type"] == MIDNIGHT_MUNCH and payload["silent"] is True and payload["url"] == "/"


@pytest.mark.parametrize("moment,protein,expected", [(at(7, 0), 1, EARLY_BIRD), (at(9, 0), 1, BEFORE_TEN),
                                                     (at(12, 0), 30, MEAL_PROTEIN)])
def test_new_types_are_not_silent_and_open_friends(client, pair, clock, fake_sender, moment, protein, expected):
    f, f_headers, _, _, _ = pair
    clock.set(moment)
    log_meal(client, f_headers, protein=protein, clock=clock)
    [(_, payload)] = fake_sender.sent
    assert payload["type"] == expected and payload["silent"] is False and payload["url"] == "/?open=friends"
    assert f["username"] in payload["body"]


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


# ---------------------------------------------------------------- database constraints and concurrency
def test_reserve_is_once_per_type_per_day_per_recipient(make_user):
    recipient, a, b, other_recipient = (make_user()[0]["id"] for _ in range(4))
    repo = SentNotificationRepository()
    db = database.SessionLocal()
    try:
        assert repo.reserve(db, recipient, a, PROTEIN_MACHINE, BASE_DAY) is True
        assert repo.reserve(db, recipient, a, PROTEIN_MACHINE, BASE_DAY) is False
        assert repo.reserve(db, recipient, b, PROTEIN_MACHINE, BASE_DAY) is False   # another friend: still no
        assert repo.reserve(db, recipient, b, BULLDOZER, BASE_DAY) is True          # another type: yes
        assert repo.reserve(db, recipient, a, PROTEIN_MACHINE, NEXT_DAY) is True    # another day: yes
        assert repo.reserve(db, other_recipient, a, PROTEIN_MACHINE, BASE_DAY) is True
        for t in (EARLY_BIRD, BEFORE_TEN, MEAL_PROTEIN, MIDNIGHT_MUNCH):            # no fixed type list in the DB
            assert repo.reserve(db, recipient, a, t, BASE_DAY) is True
        # More than the old cap of 2 per day is fine now.
        assert len([row for row in sent_rows(recipient) if row[3] == BASE_DAY]) == 6
    finally:
        db.close()


def test_concurrent_reservations_of_one_type_only_one_wins(make_user):
    recipient = make_user()[0]["id"]
    friends = [make_user()[0]["id"] for _ in range(4)]
    repo = SentNotificationRepository()
    start = threading.Barrier(12)

    def reserve(friend_id):
        db = database.SessionLocal()
        try:
            start.wait()
            return repo.reserve(db, recipient, friend_id, EARLY_BIRD, BASE_DAY)
        finally:
            db.close()

    with ThreadPoolExecutor(max_workers=12) as pool:
        outcomes = list(pool.map(reserve, friends * 3))
    assert outcomes.count(True) == 1
    assert [row[2] for row in sent_rows(recipient)] == [EARLY_BIRD]


def test_simultaneous_meals_never_send_the_same_type_twice(client, make_user, clock, fake_sender):
    """Six friends' meals are handled at the same moment for one recipient. Each meal matches
    early_bird, before_ten and meal_protein: exactly those three are sent, once each."""
    r, r_headers = make_user()
    endpoint = subscribe(client, r_headers)
    meals = []
    clock.set(at(12, 0, BASE_DAY + dt.timedelta(days=30)))   # log for BASE_DAY: nothing is sent while logging
    for _ in range(6):
        f, f_headers = make_user()
        befriend(client, f_headers, r, r_headers)
        meals.append((f["id"], log_meal(client, f_headers, protein=30, day=BASE_DAY)))
    assert fake_sender.sent == []

    start = threading.Barrier(len(meals))
    service = notifications_routes.notification_service

    def handle(meal):
        friend_id, record_id = meal
        db = database.SessionLocal()
        try:
            start.wait()
            service.handle_meal(db, friend_id, BASE_DAY, at(7, 0), record_id)
        finally:
            db.close()

    with ThreadPoolExecutor(max_workers=len(meals)) as pool:
        list(pool.map(handle, meals))
    assert sorted(fake_sender.types_to(endpoint)) == sorted([EARLY_BIRD, BEFORE_TEN, MEAL_PROTEIN])
    assert sorted(row[2] for row in sent_rows(r["id"])) == sorted([EARLY_BIRD, BEFORE_TEN, MEAL_PROTEIN])


@pytest.mark.parametrize("second,blocked", [
    ({"type": PROTEIN_MACHINE, "same_friend": True}, True),
    ({"type": PROTEIN_MACHINE, "same_friend": False}, True),   # same type from another friend
    ({"type": BULLDOZER, "same_friend": True}, False),
])
def test_database_constraint_blocks_same_type_same_day(make_user, second, blocked):
    recipient, friend, other = (make_user()[0]["id"] for _ in range(3))
    db = database.SessionLocal()
    try:
        db.add(SentNotification(recipient_id=recipient, friend_id=friend, type=PROTEIN_MACHINE, day=BASE_DAY))
        db.commit()
        db.add(SentNotification(recipient_id=recipient, friend_id=friend if second["same_friend"] else other,
                                type=second["type"], day=BASE_DAY))
        if blocked:
            with pytest.raises(IntegrityError):
                db.commit()
            db.rollback()
        else:
            db.commit()
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
