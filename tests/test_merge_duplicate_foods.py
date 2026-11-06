"""Tests for tools/merge_duplicate_foods.py, run against throwaway SQLite files with the
current schema. The real tracker.db is never opened."""
import sqlite3
from pathlib import Path

import pytest
from sqlalchemy import create_engine

from app.core.database import Base
from app.tracker.repositories import normalize_food_name as app_normalize
from conftest import REAL_DB
from tools import merge_duplicate_foods as tool
from tools.merge_duplicate_foods import MergeError, main, merge_duplicates

DATA = """
INSERT INTO users (id, username, password_hash, is_admin) VALUES (1, 'alice', 'x', 0), (2, 'bob', 'x', 0), (3, 'gil2', 'x', 1);
INSERT INTO foods (id, name, calories, protein, owner_id, suggestion_status) VALUES
    (10, 'Pro Protein Shake', 120, 20, 1, NULL),     -- alice: identical x3 (one pending) -> merged into 10
    (11, 'pro  protein shake ', 120, 20, 1, 'pending'),
    (12, 'Pro Protein Shake', 120, 20, 1, NULL),
    (13, 'Pro Protein Shake', 120, 20, 2, NULL),     -- bob: same name, other owner -> untouched
    (14, 'Pro Protein Shake', 120, 20, NULL, NULL),  -- global: same name, other pool -> untouched
    (20, 'Egg', 155, 13, NULL, NULL),                -- global identical pair -> merged into 20
    (21, 'Egg', 155, 13, NULL, NULL),
    (30, 'Bread', 250, 9, 2, NULL),                  -- bob: values differ -> listed, not merged
    (31, 'Bread', 265, 9, 2, NULL),
    (40, 'Tofu', 76, 8, 1, NULL);                    -- unique
INSERT INTO user_records (id, user_id, food_id, amount_grams, record_date) VALUES
    (100, 1, 10, 50, '2026-01-01'), (101, 1, 11, 60, '2026-01-01'), (102, 1, 12, 70, '2026-01-02'), (103, 1, 12, 10, '2026-01-03'),
    (104, 2, 13, 40, '2026-01-01'), (105, 1, 21, 100, '2026-01-01'), (106, 2, 21, 30, '2026-01-01'), (107, 3, 20, 20, '2026-01-01'),
    (108, 2, 30, 80, '2026-01-01'), (109, 2, 31, 90, '2026-01-01'), (110, 1, 40, 100, '2026-01-01');
"""


@pytest.fixture
def db(tmp_path):
    path = (tmp_path / "dupes.db").resolve()
    assert path != REAL_DB
    engine = create_engine(f"sqlite:///{path.as_posix()}")
    Base.metadata.create_all(engine)
    engine.dispose()
    with sqlite3.connect(path) as connection:
        connection.executescript(DATA)
    connection.close()
    return path


def url(path: Path) -> str:
    return f"sqlite:///{path.as_posix()}"


def query(path: Path, sql: str) -> list:
    connection = sqlite3.connect(path)
    try:
        return connection.execute(sql).fetchall()
    finally:
        connection.close()


def dump(path: Path) -> list:
    connection = sqlite3.connect(path)
    try:
        return list(connection.iterdump())
    finally:
        connection.close()


def daily_totals(path: Path) -> list:
    return query(path, """SELECT r.user_id, r.record_date, ROUND(SUM(f.calories * r.amount_grams / 100), 3),
                                 ROUND(SUM(f.protein * r.amount_grams / 100), 3), COUNT(*)
                          FROM user_records r JOIN foods f ON f.id = r.food_id GROUP BY 1, 2 ORDER BY 1, 2""")


def quiet(_line):
    pass


def test_normalization_matches_the_app():
    for name in ("  Pro  Protein Shake ", "ÉCLAIR", "שייק חלבון"):
        assert tool.normalize_food_name(name) == app_normalize(name)


def test_dry_run_reports_and_changes_nothing(db):
    before, lines = dump(db), []
    report = merge_duplicates(url(db), out=lines.append)
    assert dump(db) == before
    assert list(db.parent.glob("*.backup-*")) == []
    assert [[f.id for f in g.foods] for g in report.merged] == [[10, 11, 12], [20, 21]]
    assert [[f.id for f in g.foods] for g in report.conflicts] == [[30, 31]]
    assert (report.records_moved, report.foods_deleted) == (5, 3)
    text = "\n".join(lines)
    assert '"Pro Protein Shake" (personal of alice)' in text and '"Egg" (global)' in text
    assert "keep  #10" in text and "merge #12" in text and "2 entries" in text and "suggestion: pending" in text
    assert '"Bread" (personal of bob)' in text and "Nothing was changed" in text


def test_apply_merges_only_same_pool_same_owner_same_values(db):
    totals = daily_totals(db)
    report = merge_duplicates(url(db), apply=True, out=quiet)
    assert (report.records_moved, report.foods_deleted, report.skipped) == (5, 3, [])

    assert [row[0] for row in query(db, "SELECT id FROM foods ORDER BY id")] == [10, 13, 14, 20, 30, 31, 40]
    assert query(db, "SELECT food_id FROM user_records WHERE id IN (100, 101, 102, 103) ORDER BY id") == [(10,)] * 4
    assert query(db, "SELECT food_id FROM user_records WHERE id IN (105, 106, 107) ORDER BY id") == [(20,)] * 3
    assert query(db, "SELECT food_id FROM user_records WHERE id IN (104, 108, 109, 110) ORDER BY id") == [(13,), (30,), (31,), (40,)]
    assert query(db, "SELECT suggestion_status FROM foods WHERE id = 10") == [("pending",)]  # carried from #11
    assert daily_totals(db) == totals  # history and daily totals unchanged
    assert len(list(db.parent.glob("dupes.db.backup-*"))) == 1  # SQLite: verified backup first

    again = merge_duplicates(url(db), apply=True, out=quiet)  # idempotent
    assert again.merged == [] and [[f.id for f in g.foods] for g in again.conflicts] == [[30, 31]]


def test_group_changed_since_plan_is_skipped(db, monkeypatch):
    real_merge = tool._merge_group

    def edit_then_merge(connection, group):
        if group.keeper.id == 20:  # someone edits food #21 after the plan, before this group's transaction reads it
            other = sqlite3.connect(db)
            with other:
                other.execute("UPDATE foods SET calories = 160 WHERE id = 21")
            other.close()
        return real_merge(connection, group)

    monkeypatch.setattr(tool, "_merge_group", edit_then_merge)
    report = merge_duplicates(url(db), apply=True, out=quiet)
    assert [g.keeper.id for g in report.merged] == [10] and [g.keeper.id for g in report.skipped] == [20]
    assert query(db, "SELECT id FROM foods WHERE id IN (20, 21) ORDER BY id") == [(20,), (21,)]
    assert query(db, "SELECT food_id FROM user_records WHERE id = 105") == [(21,)]


def test_failure_rolls_back_only_that_group(db, monkeypatch):
    real_merge, calls = tool._merge_group, []

    def merge_then_fail(connection, group):
        calls.append(group.keeper.id)
        result = real_merge(connection, group)
        if group.keeper.id == 20:  # after its UPDATE and DELETE ran inside the transaction
            raise RuntimeError("simulated failure")
        return result

    monkeypatch.setattr(tool, "_merge_group", merge_then_fail)
    with pytest.raises(RuntimeError, match="simulated failure"):
        merge_duplicates(url(db), apply=True, out=quiet)
    assert calls == [10, 20]
    assert query(db, "SELECT COUNT(*) FROM foods WHERE id IN (11, 12)") == [(0,)]  # first group committed
    assert query(db, "SELECT id FROM foods WHERE id IN (20, 21) ORDER BY id") == [(20,), (21,)]  # second rolled back
    assert query(db, "SELECT food_id FROM user_records WHERE id IN (105, 106) ORDER BY id") == [(21,), (21,)]


def test_sqlite_engine_enforces_foreign_keys(db):
    from sqlalchemy.exc import IntegrityError

    engine = tool.make_engine(url(db))
    try:
        with engine.connect() as connection:
            assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1
        with pytest.raises(IntegrityError):  # deleting a food that still has entries is refused by the database
            with engine.begin() as connection:
                connection.exec_driver_sql("DELETE FROM foods WHERE id = 12")
    finally:
        engine.dispose()
    assert query(db, "SELECT COUNT(*) FROM foods WHERE id = 12") == [(1,)]


def test_recheck_moves_and_deletes_run_in_one_transaction_per_group(db, monkeypatch):
    real_merge, seen = tool._merge_group, []

    def spy(connection, group):
        dbapi = connection.connection.dbapi_connection
        statements = []
        original_execute = connection.execute

        def recording_execute(statement, *args, **kwargs):
            result = original_execute(statement, *args, **kwargs)
            statements.append((str(statement).split()[0], dbapi.in_transaction))  # (verb, inside a DB transaction?)
            return result

        monkeypatch.setattr(connection, "execute", recording_execute)
        result = real_merge(connection, group)
        seen.append((group.keeper.id, statements))
        return result

    monkeypatch.setattr(tool, "_merge_group", spy)
    merge_duplicates(url(db), apply=True, out=quiet)
    assert [keeper for keeper, _ in seen] == [10, 20]
    for _, statements in seen:
        verbs = [verb for verb, _ in statements]
        assert verbs[0] == "SELECT" and "UPDATE" in verbs and "DELETE" in verbs
        assert all(inside for _, inside in statements)  # even the first SELECT: BEGIN came before it


def test_database_url_is_required_and_env_is_ignored(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://should-never-be-used/db")
    with pytest.raises(SystemExit) as exit_info:
        main([])
    assert exit_info.value.code == 2


def test_postgres_apply_requires_backup_flag():
    with pytest.raises(MergeError, match="--i-have-a-backup"):
        merge_duplicates("postgresql://user:pw@localhost:1/none", apply=True, out=quiet)


def test_missing_database_file_is_not_created(tmp_path):
    missing = tmp_path / "missing.db"
    assert main(["--database-url", url(missing)]) == 1
    assert not missing.exists()
