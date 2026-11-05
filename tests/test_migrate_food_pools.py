"""Tests for tools/migrate_food_pools.py, run against throwaway SQLite files that
use the pre-migration schema. The real tracker.db is never opened."""
import sqlite3
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import database
from conftest import REAL_DB
from models import Food, User, UserRecord
from tools.migrate_food_pools import MigrationError, main, migrate

LEGACY_SCHEMA = """
CREATE TABLE users (id INTEGER PRIMARY KEY, username VARCHAR(64) NOT NULL UNIQUE, password_hash VARCHAR(256) NOT NULL);
CREATE TABLE foods (id INTEGER PRIMARY KEY, name VARCHAR, calories FLOAT, protein FLOAT);
CREATE TABLE user_records (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id),
    food_id INTEGER NOT NULL REFERENCES foods(id), amount_grams FLOAT NOT NULL, record_date DATE NOT NULL);
CREATE TABLE auth_sessions (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, token_hash VARCHAR(64) NOT NULL UNIQUE);
INSERT INTO users VALUES (1, 'test_user', 'x'), (2, 'test_admin', 'x'), (3, 'test_other', 'x');
INSERT INTO foods VALUES (10, 'Egg', 155, 13), (11, 'Tofu', 76, 8), (12, 'Unused', 1, 1);
INSERT INTO user_records VALUES (100, 1, 10, 50, '2026-01-01'), (101, 1, 10, 60, '2026-01-02'),
    (102, 1, 11, 70, '2026-01-01'), (103, 3, 11, 80, '2026-01-01');
"""


@pytest.fixture
def legacy_db(tmp_path):
    path = (tmp_path / "legacy.db").resolve()
    assert path != REAL_DB
    with sqlite3.connect(path) as connection:
        connection.executescript(LEGACY_SCHEMA)
    connection.close()
    return path


def url_for(path: Path) -> str:
    return f"sqlite:///{path.as_posix()}"


def columns(path: Path, table: str) -> set[str]:
    with sqlite3.connect(path) as connection:
        return {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}


def dump(path: Path) -> list:
    connection = sqlite3.connect(path)
    try:
        return list(connection.iterdump())
    finally:
        connection.close()


def backups(path: Path) -> list[Path]:
    return sorted(path.parent.glob(f"{path.name}.backup-*"))


def test_admin_argument_is_required():
    with pytest.raises(SystemExit) as exit_info:
        main([])
    assert exit_info.value.code == 2


def test_unknown_admin_changes_nothing(legacy_db):
    before = dump(legacy_db)
    with pytest.raises(MigrationError, match="does not exist"):
        migrate(url_for(legacy_db), "nobody_here", out=lambda _line: None)
    assert dump(legacy_db) == before and backups(legacy_db) == []


def test_missing_database_file_is_not_created(tmp_path):
    missing = tmp_path / "missing.db"
    with pytest.raises(MigrationError, match="not found"):
        migrate(url_for(missing), "test_admin", out=lambda _line: None)
    assert not missing.exists()


def test_dry_run_reports_single_user_foods_and_changes_nothing(legacy_db):
    before = dump(legacy_db)
    lines = []
    assert migrate(url_for(legacy_db), "test_admin", dry_run=True, out=lines.append) is None
    output = "\n".join(lines)

    assert dump(legacy_db) == before and backups(legacy_db) == []
    assert "Dry run: nothing was changed." in output
    assert 'food #10 "Egg": 2 record(s), all by test_user' in output
    assert "Tofu" not in output.split("single user")[1]  # used by two users
    assert "Unused" not in output  # no records at all
    assert "add foods.owner_id" in output and 'set is_admin = true for user "test_admin"' in output


def test_migration_adds_columns_backs_up_and_keeps_data(legacy_db):
    before = dump(legacy_db)
    backup = migrate(url_for(legacy_db), "test_admin", out=lambda _line: None)

    # Backup is a verified copy of the pre-migration database.
    assert backup is not None and backup.exists() and backups(legacy_db) == [backup]
    assert dump(backup) == before

    assert {"is_admin"} <= columns(legacy_db, "users")
    assert {"owner_id", "suggestion_status"} <= columns(legacy_db, "foods")
    with sqlite3.connect(legacy_db) as connection:
        assert connection.execute("SELECT username FROM users WHERE is_admin").fetchall() == [("test_admin",)]
        assert connection.execute("SELECT id, name, calories, protein, owner_id, suggestion_status FROM foods ORDER BY id").fetchall() == [
            (10, "Egg", 155, 13, None, None), (11, "Tofu", 76, 8, None, None), (12, "Unused", 1, 1, None, None)]
        assert connection.execute("SELECT id, user_id, food_id FROM user_records ORDER BY id").fetchall() == [
            (100, 1, 10), (101, 1, 10), (102, 1, 11), (103, 3, 11)]
        assert "ix_foods_owner_id" in {row[1] for row in connection.execute("PRAGMA index_list(foods)")}
        with pytest.raises(sqlite3.IntegrityError):  # the CHECK constraint came with the column
            connection.execute("UPDATE foods SET suggestion_status = 'bogus' WHERE id = 10")
    connection.close()

    # The app's ORM models work against the migrated file.
    engine = create_engine(url_for(legacy_db))
    try:
        with Session(engine) as db:
            assert db.get(User, 1).is_admin is False and db.get(User, 2).is_admin is True
            assert all(food.owner_id is None for food in db.query(Food))
            assert db.get(UserRecord, 103).food.name == "Tofu"
    finally:
        engine.dispose()


def test_migration_is_idempotent(legacy_db):
    migrate(url_for(legacy_db), "test_admin", out=lambda _line: None)
    after_first = dump(legacy_db)
    lines = []
    assert migrate(url_for(legacy_db), "test_admin", out=lines.append) is None
    assert "Nothing to do." in lines and dump(legacy_db) == after_first and len(backups(legacy_db)) == 1


def test_startup_check_refuses_unmigrated_database(legacy_db, monkeypatch):
    legacy_engine = create_engine(url_for(legacy_db))
    try:
        monkeypatch.setattr(database, "engine", legacy_engine)
        with pytest.raises(RuntimeError, match="migrate_food_pools"):
            database.require_food_pools_schema()
        migrate(url_for(legacy_db), "test_admin", out=lambda _line: None)
        database.require_food_pools_schema()  # passes once migrated
    finally:
        legacy_engine.dispose()
