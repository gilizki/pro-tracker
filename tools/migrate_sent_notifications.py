"""Migration: sent_notifications from "max 2 per day" to "each type once per day per recipient".

Changes, in table sent_notifications only (no other table is touched):
    - removes duplicate rows that the new rule forbids: rows with the same
      (recipient_id, type, day) about different friends. The earliest row of each
      group (lowest id = first inserted) is kept, the others are deleted
    - drops column slot and the constraints uq_sent_once_per_day,
      uq_sent_daily_slot, ck_sent_slot_positive and ck_sent_type (the fixed list of types)
    - adds constraint uq_sent_type_per_day UNIQUE (recipient_id, type, day)
These rows are only a "was it sent today" ledger; the app never displays them.
Safe to run again: an already migrated database is reported and left unchanged.

The target is the same database the app uses: $DATABASE_URL if set, otherwise
the local tracker.db next to the code.

Usage (from the project folder, with the venv's Python):
    python tools/migrate_sent_notifications.py --dry-run   # report only, changes nothing
    python tools/migrate_sent_notifications.py

BACK UP FIRST. SQLite: a verified backup (tracker.db.backup-YYYYMMDD-HHMMSS) is
written next to the database before any change, and the table is rebuilt (SQLite
cannot drop constraints in place). PostgreSQL: no backup is made here; take a
pg_dump or a platform snapshot / branch first; the table is changed with ALTER TABLE.
All changes run in one transaction.

Deploy order: run this migration, then deploy the new code (the new code refuses
to start on the old shape). Until the deploy finishes, the old code's notification
attempts fail inside its background task and are only logged; meal saving and API
responses are not affected.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Callable, Optional

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine, make_url

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.notifications.models import TYPE_PER_DAY_CONSTRAINT, outdated_sent_notifications_schema  # noqa: E402
from tools.migrate_food_pools import MigrationError, _make_engine, backup_sqlite  # noqa: E402

TABLE = "sent_notifications"

DUPLICATES_WHERE = f"id NOT IN (SELECT MIN(id) FROM {TABLE} GROUP BY recipient_id, type, day)"

SQLITE_NEW_TABLE = f"""
CREATE TABLE {TABLE}_new (
    id INTEGER NOT NULL PRIMARY KEY,
    recipient_id INTEGER NOT NULL REFERENCES users (id),
    friend_id INTEGER NOT NULL REFERENCES users (id),
    type VARCHAR(32) NOT NULL,
    day DATE NOT NULL,
    created_at DATETIME NOT NULL,
    CONSTRAINT {TYPE_PER_DAY_CONSTRAINT} UNIQUE (recipient_id, type, day),
    CONSTRAINT ck_sent_not_self CHECK (recipient_id <> friend_id)
)"""


def _steps(dialect: str, has_unique: bool) -> list[tuple[str, str]]:
    delete = ("delete duplicate rows (same recipient, type and day; keep the earliest)",
              f"DELETE FROM {TABLE} WHERE {DUPLICATES_WHERE}")
    if dialect == "sqlite":
        return [
            delete,
            ("create the new table shape", SQLITE_NEW_TABLE),
            ("copy every remaining row",
             f"INSERT INTO {TABLE}_new (id, recipient_id, friend_id, type, day, created_at) "
             f"SELECT id, recipient_id, friend_id, type, day, created_at FROM {TABLE}"),
            ("drop the old table", f"DROP TABLE {TABLE}"),
            ("rename the new table", f"ALTER TABLE {TABLE}_new RENAME TO {TABLE}"),
        ]
    steps = [delete] + [
        (f"drop constraint {name}", f"ALTER TABLE {TABLE} DROP CONSTRAINT IF EXISTS {name}")
        for name in ("uq_sent_once_per_day", "uq_sent_daily_slot", "ck_sent_slot_positive", "ck_sent_type")
    ]
    steps.append(("drop column slot", f"ALTER TABLE {TABLE} DROP COLUMN IF EXISTS slot"))
    if not has_unique:
        steps.append((f"add constraint {TYPE_PER_DAY_CONSTRAINT} UNIQUE (recipient_id, type, day)",
                      f"ALTER TABLE {TABLE} ADD CONSTRAINT {TYPE_PER_DAY_CONSTRAINT} UNIQUE (recipient_id, type, day)"))
    return steps


def _count(connection, where: str = "1 = 1") -> int:
    return connection.execute(text(f"SELECT COUNT(*) FROM {TABLE} WHERE {where}")).scalar_one()


def migrate(database_url: str, dry_run: bool = False, out: Callable[[str], None] = print) -> Optional[Path]:
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

    engine: Engine = _make_engine(url.render_as_string(hide_password=False))
    try:
        inspector = inspect(engine)
        if TABLE not in inspector.get_table_names():
            out(f"Table {TABLE} does not exist; the app creates it in the new shape. Nothing to do.")
            return None
        problems = outdated_sent_notifications_schema(inspector)
        if not problems:
            out("Planned changes: none (already migrated). Nothing to do.")
            return None
        has_unique = TYPE_PER_DAY_CONSTRAINT in {u["name"] for u in inspector.get_unique_constraints(TABLE)}
        steps = _steps(engine.dialect.name, has_unique)

        with engine.connect() as connection:
            rows_before = _count(connection)
            duplicates = _count(connection, DUPLICATES_WHERE)
            groups = connection.execute(text(f"""
                SELECT recipient_id, type, day, COUNT(*) AS n, MIN(id) AS kept FROM {TABLE}
                GROUP BY recipient_id, type, day HAVING COUNT(*) > 1 ORDER BY day, recipient_id, type
            """)).mappings().all()

        out(f"Old shape found: {', '.join(problems)}")
        out(f"Rows: {rows_before}; duplicate rows to delete: {duplicates}")
        for group in groups:
            out(f"  - recipient {group['recipient_id']}, {group['type']}, {group['day']}: "
                f"{group['n']} rows, keeping id {group['kept']}")
        out("Planned changes:")
        for description, _sql in steps:
            out(f"  - {description}")

        if dry_run:
            out("Dry run: nothing was changed.")
            return None

        backup = None
        if db_path is not None:
            backup = backup_sqlite(db_path)
            out(f"Backup written and verified: {backup}")
        else:
            out("REMINDER: no file backup is made for this database type; "
                "make sure you took a pg_dump / snapshot / branch first.")

        with engine.begin() as connection:  # one transaction: all or nothing
            for _description, sql in steps:
                connection.execute(text(sql))

        # Verify the result before reporting success.
        remaining = outdated_sent_notifications_schema(inspect(engine))
        if remaining:
            raise MigrationError(f"Verification failed after commit: still {remaining}. Backup: {backup}")
        with engine.connect() as connection:
            rows_after = _count(connection)
        if rows_after != rows_before - duplicates:
            raise MigrationError(f"Verification failed after commit: expected {rows_before - duplicates} rows, "
                                 f"found {rows_after}. Backup: {backup}")
        out(f"Migration complete. Rows: {rows_before} -> {rows_after} ({duplicates} duplicates removed).")
        return backup
    finally:
        engine.dispose()


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="report what would change; change nothing")
    args = parser.parse_args(argv)

    from app.core.database import DATABASE_URL  # same target as the app: $DATABASE_URL or the local tracker.db

    try:
        migrate(DATABASE_URL, dry_run=args.dry_run)
    except MigrationError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
