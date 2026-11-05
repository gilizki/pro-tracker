"""Tests for tools/migrate_sent_notifications.py (SQLite path), run against throwaway
SQLite files in the OLD (max-2-per-day) shape. The real tracker.db is never opened.

The PostgreSQL path (ALTER TABLE) is NOT exercised here: there is no PostgreSQL in
the test environment.
"""
import datetime as dt
import sqlite3
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import Session

import tools.migrate_sent_notifications as migration
from app.core import database
from app.core.database import Base
from app.notifications.models import outdated_sent_notifications_schema
from app.notifications.repository import SentNotificationRepository
from conftest import REAL_DB
from tools.migrate_sent_notifications import MigrationError, main, migrate

OLD_SCHEMA = """
CREATE TABLE users (id INTEGER PRIMARY KEY, username VARCHAR(64) NOT NULL UNIQUE, password_hash VARCHAR(256) NOT NULL);
INSERT INTO users VALUES (1, 'test_r1', 'x'), (2, 'test_f2', 'x'), (3, 'test_f3', 'x'), (4, 'test_r4', 'x');
CREATE TABLE sent_notifications (
    id INTEGER PRIMARY KEY,
    recipient_id INTEGER NOT NULL REFERENCES users(id),
    friend_id INTEGER NOT NULL REFERENCES users(id),
    type VARCHAR(32) NOT NULL,
    day DATE NOT NULL,
    slot INTEGER NOT NULL,
    created_at DATETIME NOT NULL,
    CONSTRAINT uq_sent_once_per_day UNIQUE (recipient_id, friend_id, type, day),
    CONSTRAINT uq_sent_daily_slot UNIQUE (recipient_id, day, slot),
    CONSTRAINT ck_sent_not_self CHECK (recipient_id <> friend_id),
    CONSTRAINT ck_sent_slot_positive CHECK (slot >= 1),
    CONSTRAINT ck_sent_type CHECK (type IN ('protein_machine', 'midnight_munch', 'bulldozer'))
);
-- ids 1+2 and 4+5 collide under the new rule (same recipient, type and day, other friend).
INSERT INTO sent_notifications VALUES
    (1, 1, 2, 'protein_machine', '2026-11-10', 1, '2026-11-10 09:00:00'),
    (2, 1, 3, 'protein_machine', '2026-11-10', 2, '2026-11-10 10:00:00'),
    (3, 1, 2, 'bulldozer',       '2026-11-11', 1, '2026-11-11 09:00:00'),
    (4, 4, 2, 'midnight_munch',  '2026-11-10', 1, '2026-11-10 23:00:00'),
    (5, 4, 3, 'midnight_munch',  '2026-11-10', 2, '2026-11-10 23:30:00'),
    (6, 1, 2, 'protein_machine', '2026-11-11', 2, '2026-11-11 12:00:00');
"""
KEPT = [(1, 1, 2, "protein_machine", "2026-11-10"), (3, 1, 2, "bulldozer", "2026-11-11"),
        (4, 4, 2, "midnight_munch", "2026-11-10"), (6, 1, 2, "protein_machine", "2026-11-11")]


@pytest.fixture
def old_db(tmp_path):
    path = (tmp_path / "old.db").resolve()
    assert path != REAL_DB
    connection = sqlite3.connect(path)
    connection.executescript(OLD_SCHEMA)
    connection.close()
    return path


def url_for(path: Path) -> str:
    return f"sqlite:///{path.as_posix()}"


def dump(path: Path) -> list:
    connection = sqlite3.connect(path)
    try:
        return list(connection.iterdump())
    finally:
        connection.close()


def rows(path: Path) -> list:
    connection = sqlite3.connect(path)
    try:
        return connection.execute("SELECT id, recipient_id, friend_id, type, day FROM sent_notifications ORDER BY id").fetchall()
    finally:
        connection.close()


def backups(path: Path) -> list[Path]:
    return sorted(path.parent.glob(f"{path.name}.backup-*"))


def shape(url: str) -> dict:
    engine = create_engine(url)
    try:
        inspector = inspect(engine)
        table = "sent_notifications"
        return {
            "columns": [(c["name"], c["nullable"]) for c in inspector.get_columns(table)],
            "uniques": sorted((u["name"], tuple(u["column_names"])) for u in inspector.get_unique_constraints(table)),
            "checks": sorted(c["name"] for c in inspector.get_check_constraints(table)),
            "pk": inspector.get_pk_constraint(table)["constrained_columns"],
            "fks": sorted((tuple(f["constrained_columns"]), f["referred_table"]) for f in inspector.get_foreign_keys(table)),
        }
    finally:
        engine.dispose()


def quiet(_line):
    pass


def test_dry_run_reports_duplicates_and_changes_nothing(old_db):
    before = dump(old_db)
    lines = []
    assert migrate(url_for(old_db), dry_run=True, out=lines.append) is None
    output = "\n".join(lines)
    assert "Rows: 6; duplicate rows to delete: 2" in output
    assert "recipient 1, protein_machine, 2026-11-10: 2 rows, keeping id 1" in output
    assert "recipient 4, midnight_munch, 2026-11-10: 2 rows, keeping id 4" in output
    assert "Dry run: nothing was changed." in output
    assert dump(old_db) == before and backups(old_db) == []


def test_main_dry_run_targets_the_app_database_and_returns_0(capsys):
    # In tests the app database is the temp test DB (already in the new shape).
    assert main(["--dry-run"]) == 0
    assert "Nothing to do" in capsys.readouterr().out


def test_migration_dedupes_keeps_earliest_and_backs_up(old_db):
    backup = migrate(url_for(old_db), out=quiet)
    assert backup is not None and backups(old_db) == [backup]
    assert len(rows(backup)) == 6 and "slot" in {c[0] for c in shape(url_for(backup))["columns"]}  # untouched copy
    assert rows(old_db) == KEPT
    engine = create_engine(url_for(old_db))
    try:
        assert outdated_sent_notifications_schema(inspect(engine)) == []
    finally:
        engine.dispose()


def test_migrated_table_matches_a_fresh_database(old_db, tmp_path):
    migrate(url_for(old_db), out=quiet)
    fresh = (tmp_path / "fresh.db").resolve()
    engine = create_engine(url_for(fresh))
    Base.metadata.create_all(engine)
    engine.dispose()
    assert shape(url_for(old_db)) == shape(url_for(fresh))
    assert "slot" not in {c[0] for c in shape(url_for(old_db))["columns"]}


def test_migrated_table_enforces_the_new_rule(old_db):
    migrate(url_for(old_db), out=quiet)
    engine = create_engine(url_for(old_db))
    repo = SentNotificationRepository()
    try:
        with Session(engine) as db:
            day = dt.date(2026, 11, 10)
            assert repo.reserve(db, 1, 3, "protein_machine", day) is False  # kept row id 1 already holds it
            assert repo.reserve(db, 1, 3, "early_bird", day) is True        # new types: no CHECK any more
            assert repo.reserve(db, 1, 2, "early_bird", day) is False       # same type, other friend
            assert repo.reserve(db, 1, 3, "meal_protein", day) is True      # more than 2 per day is fine
    finally:
        engine.dispose()


def test_migration_is_idempotent(old_db):
    migrate(url_for(old_db), out=quiet)
    after_first = dump(old_db)
    lines = []
    assert migrate(url_for(old_db), out=lines.append) is None
    assert any("Nothing to do" in line for line in lines)
    assert dump(old_db) == after_first and len(backups(old_db)) == 1


def test_failure_mid_migration_rolls_everything_back(old_db, monkeypatch):
    real_steps = migration._steps

    def failing_steps(dialect, has_unique):
        steps = real_steps(dialect, has_unique)
        return steps[:-1] + [("boom", "SELECT * FROM no_such_table")] + steps[-1:]

    monkeypatch.setattr(migration, "_steps", failing_steps)
    before = dump(old_db)
    with pytest.raises(Exception):
        migrate(url_for(old_db), out=quiet)
    assert dump(old_db) == before   # the DELETE, CREATE, INSERT and DROP were all rolled back
    assert len(backups(old_db)) == 1


def test_missing_database_file_is_not_created(tmp_path):
    missing = tmp_path / "missing.db"
    with pytest.raises(MigrationError, match="not found"):
        migrate(url_for(missing), out=quiet)
    assert not missing.exists()


def test_database_without_the_table_is_left_alone(tmp_path):
    path = (tmp_path / "empty.db").resolve()
    sqlite3.connect(path).close()
    lines = []
    assert migrate(url_for(path), out=lines.append) is None
    assert any("Nothing to do" in line for line in lines) and backups(path) == []


def test_startup_check_refuses_old_shape_until_migrated(old_db, monkeypatch):
    old_engine = create_engine(url_for(old_db))
    try:
        monkeypatch.setattr(database, "engine", old_engine)
        with pytest.raises(RuntimeError, match="migrate_sent_notifications"):
            database.require_sent_notifications_schema()
        assert dump(old_db)  # the check changed nothing: still the old shape
        assert "slot" in {c[0] for c in shape(url_for(old_db))["columns"]}
        migrate(url_for(old_db), out=quiet)
        database.require_sent_notifications_schema()  # passes once migrated
    finally:
        old_engine.dispose()
