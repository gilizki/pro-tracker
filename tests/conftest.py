"""Shared test setup.

SAFETY: the tests must never touch the real tracker.db.  This file points
DATABASE_URL at a fresh SQLite file in a new temp directory BEFORE any app
module is imported (which is when the engine is created), then verifies the
engine really uses that temp file.  If it does not, the whole run aborts
instead of falling back to the default database.
"""
import os
import sys
import tempfile
import uuid
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
REAL_DB = (PROJECT_ROOT / "tracker.db").resolve()

TEST_DIR = Path(tempfile.mkdtemp(prefix="pro_tracker_tests_")).resolve()
TEST_DB = TEST_DIR / "test.db"
os.environ["DATABASE_URL"] = f"sqlite:///{TEST_DB.as_posix()}"

# main.py serves frontend/ relative to the working directory.
os.chdir(PROJECT_ROOT)
sys.path.insert(0, str(PROJECT_ROOT))


def _assert_engine_is_temp_db() -> None:
    import database

    db_path = database.engine.url.database
    if not db_path or database.engine.url.get_backend_name() != "sqlite":
        pytest.exit("Refusing to run: test engine is not the temp SQLite DB", returncode=1)
    resolved = Path(db_path).resolve()
    if resolved == REAL_DB or resolved != TEST_DB or TEST_DIR not in resolved.parents:
        pytest.exit(f"Refusing to run: tests would use {resolved}, not the temp DB {TEST_DB}", returncode=1)


_assert_engine_is_temp_db()

from fastapi.testclient import TestClient  # noqa: E402

import main  # noqa: E402  (imported only after the safety check)


@pytest.fixture(scope="session")
def client():
    _assert_engine_is_temp_db()
    with TestClient(main.app) as test_client:
        yield test_client


def new_username() -> str:
    return f"u_{uuid.uuid4().hex[:12]}"


@pytest.fixture
def make_user(client):
    """Signs up a fresh user and returns (user dict, auth headers)."""
    def _make():
        response = client.post("/auth/signup", json={"username": new_username(), "password": "password123"})
        assert response.status_code == 200, response.text
        data = response.json()
        return data["user"], {"Authorization": f"Bearer {data['token']}"}
    return _make
