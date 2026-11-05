"""The 02:00 Israel-time day boundary.

These tests run the REAL frontend/day.js (the single source of the rule) in Node,
rather than re-implementing it in Python.  Node is not a project dependency, so
they are skipped when it is not installed.
"""
import datetime as dt
import json
import shutil
import subprocess

import pytest

from app.friends import service as friends_service
from conftest import PROJECT_ROOT

NODE = shutil.which("node")
DAY_JS = PROJECT_ROOT / "frontend" / "day.js"
pytestmark = pytest.mark.skipif(NODE is None, reason="Node.js is not installed")

# (description, UTC instant, expected tracker day). Israel: IST = UTC+2, IDT = UTC+3.
# 2026 DST: starts Fri Mar 27 at 02:00 (-> 03:00), ends Sun Oct 25 at 02:00 (-> 01:00).
CASES = [
    ("winter 01:59 IST", "2026-11-02T23:59:00Z", "2026-11-02"),
    ("winter 02:00 IST", "2026-11-03T00:00:00Z", "2026-11-03"),
    ("summer 01:59 IDT", "2026-07-14T22:59:00Z", "2026-07-14"),
    ("summer 02:00 IDT", "2026-07-14T23:00:00Z", "2026-07-15"),
    ("summer 02:30 IDT (UTC date is still the 14th)", "2026-07-14T23:30:00Z", "2026-07-15"),
    ("fall-back night, first 01:30 (IDT)", "2026-10-24T22:30:00Z", "2026-10-24"),
    ("fall-back night, second 01:30 (IST)", "2026-10-24T23:30:00Z", "2026-10-24"),
    ("fall-back night, 02:00 IST", "2026-10-25T00:00:00Z", "2026-10-25"),
    ("spring-forward night, 01:59 IST", "2026-03-26T23:59:00Z", "2026-03-26"),
    ("spring-forward night, 03:00 IDT (02:xx skipped)", "2026-03-27T00:00:00Z", "2026-03-27"),
    ("month boundary, 01:00 on Mar 1", "2026-02-28T23:00:00Z", "2026-02-28"),
    ("year boundary, 01:00 on Jan 1", "2025-12-31T23:00:00Z", "2025-12-31"),
]


def tracker_days(instants, tz_env=None):
    """Runs frontend/day.js in Node and returns trackerDay() for each UTC instant."""
    script = (
        "require(process.argv[1]);"
        "const instants = JSON.parse(process.argv[2]);"
        "console.log(JSON.stringify(instants.map(i => trackerDay(new Date(i)))));"
    )
    env = None
    if tz_env:
        import os
        env = {**os.environ, "TZ": tz_env}
    out = subprocess.run([NODE, "-e", script, str(DAY_JS), json.dumps(instants)],
                         capture_output=True, text=True, check=True, env=env)
    return json.loads(out.stdout)


@pytest.mark.parametrize("name,instant,expected", CASES, ids=[c[0] for c in CASES])
def test_day_boundary(name, instant, expected):
    assert tracker_days([instant]) == [expected]


@pytest.mark.parametrize("device_tz", ["America/New_York", "Asia/Tokyo", "UTC"])
def test_device_time_zone_is_ignored(device_tz):
    """Always Israel time, whatever the phone's own time zone is set to."""
    instants = [c[1] for c in CASES]
    assert tracker_days(instants, tz_env=device_tz) == [c[2] for c in CASES]


@pytest.mark.parametrize("instant", ["2026-11-02T23:59:00Z", "2026-11-03T00:00:00Z",
                                     "2026-07-14T22:59:00Z", "2026-07-14T23:30:00Z"])
def test_friends_endpoint_accepts_tracker_day(client, make_user, monkeypatch, instant):
    """At these moments (winter and summer), the day.js date passes the server's +-1 check,
    and the friend's totals are read for exactly that tracker day."""
    [day] = tracker_days([instant])
    utc_now = dt.datetime.fromisoformat(instant.replace("Z", "+00:00"))
    monkeypatch.setattr(friends_service, "_utc_today", lambda: utc_now.date())

    a, a_headers = make_user()
    b, b_headers = make_user()
    sent = client.post("/friends/requests", json={"username": b["username"]}, headers=a_headers)
    assert sent.status_code == 200
    request_id = client.get("/friends/requests", headers=b_headers).json()["incoming"][0]["id"]
    client.post(f"/friends/requests/{request_id}/accept", headers=b_headers)

    food = client.post("/foods/", json={"name": "BoundaryFood", "calories": 100, "protein": 10}, headers=b_headers).json()
    client.post("/records/", json={"food_id": food["id"], "amount_grams": 100, "date": day}, headers=b_headers)

    status = client.get(f"/friends/{b['id']}/status", params={"date": day}, headers=a_headers)
    assert status.status_code == 200, status.text
    assert status.json()["date"] == day
    assert status.json()["total_calories"] == 100.0 and status.json()["total_protein"] == 10.0
