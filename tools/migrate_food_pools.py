"""Migration: split the food catalogue into a global pool and per-user personal pools.

Adds (no data is changed or removed):
    users.is_admin            BOOLEAN NOT NULL DEFAULT false
    foods.owner_id            INTEGER NULL REFERENCES users(id)   -- NULL = global
    foods.suggestion_status   VARCHAR(16) NULL ('pending' / 'rejected')
    index ix_foods_owner_id
and sets is_admin for the ONE user named by --admin. Every existing food keeps
owner_id = NULL, so it stays global. Safe to run again: existing columns are skipped.

The target is the same database the app uses: $DATABASE_URL if set, otherwise
the local tracker.db next to the code. Nothing else is ever touched.

Usage (from the project folder, with the venv's Python):
    python tools/migrate_food_pools.py --admin <username> --dry-run   # report only, changes nothing
    python tools/migrate_food_pools.py --admin <username>

SQLite: a verified backup (tracker.db.backup-YYYYMMDD-HHMMSS) is written next to
the database before any change. PostgreSQL: no backup is made here; take a
pg_dump or platform snapshot first. All changes run in one transaction.
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.engine import Engine, make_url

PROJECT_ROOT = Path(__file__).resolve().parent.parent
REQUIRED_TABLES = ("users", "foods", "user_records")
SUGGESTION_CHECK = "suggestion_status IS NULL OR suggestion_status IN ('pending', 'rejected')"


class MigrationError(Exception):
    """A precondition failed. Raised before any change is made."""


def _make_engine(database_url: str) -> Engine:
    engine = create_engine(database_url)
    if engine.dialect.name == "sqlite":
        # pysqlite runs DDL outside transactions by default; take control so the
        # ALTERs and the UPDATE commit or roll back together (SQLAlchemy recipe).
        @event.listens_for(engine, "connect")
        def _no_implicit_transactions(dbapi_connection, _record):
            dbapi_connection.isolation_level = None

        @event.listens_for(engine, "begin")
        def _explicit_begin(connection):
            connection.exec_driver_sql("BEGIN")
    return engine


def _planned_steps(engine: Engine) -> list[tuple[str, str]]:
    inspector = inspect(engine)
    user_columns = {c["name"] for c in inspector.get_columns("users")}
    food_columns = {c["name"] for c in inspector.get_columns("foods")}
    food_indexes = {i["name"] for i in inspector.get_indexes("foods")}
    false_literal = "0" if engine.dialect.name == "sqlite" else "FALSE"

    steps = []
    if "is_admin" not in user_columns:
        steps.append(("add users.is_admin (default false)",
                      f"ALTER TABLE users ADD COLUMN is_admin BOOLEAN NOT NULL DEFAULT {false_literal}"))
    if "owner_id" not in food_columns:
        steps.append(("add foods.owner_id (NULL = global; every existing food stays global)",
                      "ALTER TABLE foods ADD COLUMN owner_id INTEGER REFERENCES users(id)"))
    if "suggestion_status" not in food_columns:
        steps.append(("add foods.suggestion_status (NULL)",
                      "ALTER TABLE foods ADD COLUMN suggestion_status VARCHAR(16) "
                      f"CONSTRAINT ck_foods_suggestion_status CHECK ({SUGGESTION_CHECK})"))
    if "ix_foods_owner_id" not in food_indexes:
        steps.append(("add index ix_foods_owner_id", "CREATE INDEX IF NOT EXISTS ix_foods_owner_id ON foods (owner_id)"))
    return steps


def _row_counts(connection) -> dict[str, int]:
    return {table: connection.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar_one() for table in REQUIRED_TABLES}


def single_user_foods(engine: Engine) -> list[dict]:
    """Global foods whose meal records all belong to one user (candidates to move to that user's pool)."""
    has_owner = "owner_id" in {c["name"] for c in inspect(engine).get_columns("foods")}
    only_global = "WHERE f.owner_id IS NULL" if has_owner else ""
    query = text(f"""
        SELECT f.id AS food_id, f.name AS food_name, u.username AS username, s.records AS records
        FROM (SELECT food_id, MIN(user_id) AS user_id, COUNT(*) AS records
              FROM user_records GROUP BY food_id HAVING COUNT(DISTINCT user_id) = 1) s
        JOIN foods f ON f.id = s.food_id
        JOIN users u ON u.id = s.user_id
        {only_global}
        ORDER BY u.username, f.name, f.id
    """)
    with engine.connect() as connection:
        return [dict(row) for row in connection.execute(query).mappings()]


def backup_sqlite(db_path: Path) -> Path:
    """Copies the database with SQLite's online backup API and verifies the copy."""
    destination = db_path.with_name(f"{db_path.name}.backup-{datetime.now():%Y%m%d-%H%M%S}")
    if destination.exists():
        raise MigrationError(f"Backup file already exists: {destination}")
    source = sqlite3.connect(f"{db_path.as_uri()}?mode=ro", uri=True)
    copy = sqlite3.connect(destination)
    try:
        source.backup(copy)
        if copy.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise MigrationError(f"Backup failed its integrity check: {destination}")
        tables = [row[0] for row in source.execute("SELECT name FROM sqlite_master WHERE type = 'table'")]
        for table in tables:
            quoted = '"' + table.replace('"', '""') + '"'
            if source.execute(f"SELECT COUNT(*) FROM {quoted}").fetchone() != copy.execute(f"SELECT COUNT(*) FROM {quoted}").fetchone():
                raise MigrationError(f"Backup row count mismatch in table {table}: {destination}")
    finally:
        copy.close()
        source.close()
    return destination


def migrate(database_url: str, admin_username: str, dry_run: bool = False,
            out: Callable[[str], None] = print) -> Optional[Path]:
    """Runs the migration. Returns the SQLite backup path (None if no backup was made)."""
    url = make_url(database_url.replace("postgres://", "postgresql://", 1))
    out(f"Target database: {url.render_as_string(hide_password=True)}")

    db_path = None
    if url.get_backend_name() == "sqlite":
        if not url.database or url.database == ":memory:":
            raise MigrationError("An SQLite migration needs a database file")
        db_path = Path(url.database).resolve()
        if not db_path.is_file():  # create_engine would silently create an empty database
            raise MigrationError(f"Database file not found: {db_path}")

    engine = _make_engine(url.render_as_string(hide_password=False))
    try:
        missing = [t for t in REQUIRED_TABLES if t not in inspect(engine).get_table_names()]
        if missing:
            raise MigrationError(f"Missing tables {missing}; is this the Pro Tracker database?")

        with engine.connect() as connection:
            admin = connection.execute(text("SELECT id FROM users WHERE username = :username"),
                                       {"username": admin_username}).first()
        if admin is None:
            raise MigrationError(f'User "{admin_username}" does not exist in the target database. Nothing was changed.')

        steps = _planned_steps(engine)
        has_is_admin = "is_admin" in {c["name"] for c in inspect(engine).get_columns("users")}
        with engine.connect() as connection:
            already_admin = has_is_admin and bool(connection.execute(
                text("SELECT is_admin FROM users WHERE id = :id"), {"id": admin.id}).scalar_one())
            counts_before = _row_counts(connection)

        out(f"Rows: {counts_before}")
        out("Planned changes:" if steps or not already_admin else "Planned changes: none (already migrated)")
        for description, _sql in steps:
            out(f"  - {description}")
        if not already_admin:
            out(f'  - set is_admin = true for user "{admin_username}" (id {admin.id})')

        if dry_run:
            candidates = single_user_foods(engine)
            out("")
            out("Global foods whose meal records all belong to a single user"
                " (candidates for that user's personal pool; nothing is moved automatically):")
            if not candidates:
                out("  (none)")
            for row in candidates:
                out(f'  - food #{row["food_id"]} "{row["food_name"]}": {row["records"]} record(s), all by {row["username"]}')
            out("")
            out("Dry run: nothing was changed.")
            return None

        if not steps and already_admin:
            out("Nothing to do.")
            return None

        backup = None
        if db_path is not None:
            backup = backup_sqlite(db_path)
            out(f"Backup written and verified: {backup}")
        else:
            out("No file backup for this database type; make sure you took a snapshot / pg_dump first.")

        with engine.begin() as connection:  # one transaction: all or nothing
            for _description, sql in steps:
                connection.execute(text(sql))
            connection.execute(text("UPDATE users SET is_admin = :value WHERE id = :id"), {"value": True, "id": admin.id})

        # Verify the result before reporting success.
        if _planned_steps(engine):
            raise MigrationError(f"Verification failed after commit: columns or indexes still missing. Backup: {backup}")
        with engine.connect() as connection:
            counts_after = _row_counts(connection)
            admins = [row[0] for row in connection.execute(
                text("SELECT username FROM users WHERE is_admin = :value ORDER BY username"), {"value": True})]
        if counts_after != counts_before:
            raise MigrationError(f"Verification failed after commit: row counts changed {counts_before} -> {counts_after}. Backup: {backup}")
        out(f"Migration complete. Rows unchanged: {counts_after}. Admin users: {admins}")
        return backup
    finally:
        engine.dispose()


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--admin", required=True, metavar="USERNAME", help="the one existing user to mark as admin")
    parser.add_argument("--dry-run", action="store_true", help="report what would change; change nothing")
    args = parser.parse_args(argv)

    sys.path.insert(0, str(PROJECT_ROOT))
    from database import DATABASE_URL  # same target as the app: $DATABASE_URL or the local tracker.db

    try:
        migrate(DATABASE_URL, args.admin, dry_run=args.dry_run)
    except MigrationError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
