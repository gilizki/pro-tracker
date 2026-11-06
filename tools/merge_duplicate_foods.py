"""Merge duplicate foods (the ones the food list shows as "Name #2").

Duplicates are foods in the SAME pool with the same name (case and extra spaces ignored):
both global, or both personal foods of the same user. A global and a personal food with the
same name are never merged, and neither are two different users' foods.

For each group whose foods all have the same calories and protein, the oldest food (lowest id)
is kept, the log entries of the others are moved onto it, and the others are deleted, in one
transaction per group, so meal history and daily totals do not change. If an extra was a pending
suggestion and the kept food is not suggested, the kept food becomes the pending suggestion.
Groups whose calories or protein differ are only listed, for you to decide; they are never merged.

DRY RUN BY DEFAULT: it prints the plan and changes nothing. --apply does the real run.

The database must be given explicitly with --database-url; $DATABASE_URL and the local
tracker.db are never used on their own. Usage (from the project folder):
    python tools/merge_duplicate_foods.py --database-url sqlite:///tracker.db            # dry run
    python tools/merge_duplicate_foods.py --database-url sqlite:///tracker.db --apply    # SQLite: backs up first
    python tools/merge_duplicate_foods.py --database-url "postgresql://..." --apply --i-have-a-backup

PostgreSQL: no backup is made here. Take a Neon branch / pg_dump first; --apply refuses to run
on PostgreSQL without --i-have-a-backup.
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from sqlalchemy import bindparam, create_engine, inspect, text
from sqlalchemy.engine import Connection, make_url

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tools.migrate_food_pools import backup_sqlite  # noqa: E402

REQUIRED_TABLES = ("users", "foods", "user_records")


class MergeError(Exception):
    """A precondition failed. Raised before any change is made."""


def normalize_food_name(name: str) -> str:
    """Same key as app.tracker.repositories.normalize_food_name (a test keeps them equal)."""
    return " ".join(name.split()).casefold()


@dataclass
class FoodRow:
    id: int
    name: str
    calories: float
    protein: float
    owner_id: Optional[int]
    owner: Optional[str]
    suggestion_status: Optional[str]
    records: int


@dataclass
class Group:
    foods: list[FoodRow]  # sorted by id: foods[0] is the one kept

    @property
    def keeper(self) -> FoodRow:
        return self.foods[0]

    @property
    def extras(self) -> list[FoodRow]:
        return self.foods[1:]

    @property
    def mergeable(self) -> bool:
        return len({(f.calories, f.protein) for f in self.foods}) == 1

    @property
    def pool(self) -> str:
        return "global" if self.keeper.owner_id is None else f"personal of {self.keeper.owner}"


@dataclass
class Report:
    merged: list[Group] = field(default_factory=list)  # merged (with --apply) or would be merged (dry run)
    conflicts: list[Group] = field(default_factory=list)  # values differ: listed only
    skipped: list[Group] = field(default_factory=list)  # changed since planning (--apply only): left as is
    records_moved: int = 0
    foods_deleted: int = 0


def find_groups(connection: Connection) -> list[Group]:
    rows = connection.execute(text("""
        SELECT f.id, f.name, f.calories, f.protein, f.owner_id, u.username, f.suggestion_status,
               (SELECT COUNT(*) FROM user_records r WHERE r.food_id = f.id) AS records
        FROM foods f LEFT JOIN users u ON u.id = f.owner_id
        ORDER BY f.id""")).all()
    # Grouped in Python: SQL lower() differs between SQLite and PostgreSQL for non-ASCII (e.g. Hebrew) text.
    groups: dict[tuple, list[FoodRow]] = {}
    for row in rows:
        food = FoodRow(*row)
        groups.setdefault((food.owner_id, normalize_food_name(food.name)), []).append(food)
    return [Group(foods) for foods in groups.values() if len(foods) > 1]


def _merge_group(connection: Connection, planned: Group) -> Optional[tuple[int, int]]:
    """One group in the caller's transaction. Re-reads the foods (locked on PostgreSQL) and returns None,
    changing nothing, if they changed since the plan. Returns (records moved, foods deleted)."""
    ids = [f.id for f in planned.foods]
    current = connection.execute(
        text("SELECT id, name, calories, protein, owner_id, suggestion_status FROM foods WHERE id IN :ids ORDER BY id"
             + (" FOR UPDATE" if connection.dialect.name == "postgresql" else ""))
        .bindparams(bindparam("ids", expanding=True)), {"ids": ids}).all()
    expected = [(f.id, f.name, f.calories, f.protein, f.owner_id, f.suggestion_status) for f in planned.foods]
    if [tuple(row) for row in current] != expected:
        return None
    keeper, extra_ids = planned.keeper, [f.id for f in planned.extras]
    moved = connection.execute(text("UPDATE user_records SET food_id = :keeper WHERE food_id IN :extras")
                               .bindparams(bindparam("extras", expanding=True)),
                               {"keeper": keeper.id, "extras": extra_ids}).rowcount
    if keeper.suggestion_status is None and any(f.suggestion_status == "pending" for f in planned.extras):
        connection.execute(text("UPDATE foods SET suggestion_status = 'pending' WHERE id = :id"), {"id": keeper.id})
    deleted = connection.execute(text("DELETE FROM foods WHERE id IN :extras")
                                 .bindparams(bindparam("extras", expanding=True)), {"extras": extra_ids}).rowcount
    left = connection.execute(text("SELECT COUNT(*) FROM user_records WHERE food_id IN :extras")
                              .bindparams(bindparam("extras", expanding=True)), {"extras": extra_ids}).scalar_one()
    if deleted != len(extra_ids) or left:
        raise MergeError(f"Group of food #{keeper.id} did not merge cleanly; rolled back")
    return moved, deleted


def _describe(group: Group, out: Callable[[str], None], action: bool) -> None:
    out(f'  "{group.keeper.name}" ({group.pool})')
    for index, food in enumerate(group.foods):
        role = ("keep  " if index == 0 else "merge ") if action else "      "
        suggestion = f", suggestion: {food.suggestion_status}" if food.suggestion_status else ""
        out(f"    {role}#{food.id} \"{food.name}\": {food.calories:g} kcal, {food.protein:g} g protein, "
            f"{food.records} entr{'y' if food.records == 1 else 'ies'}{suggestion}")


def merge_duplicates(database_url: str, apply: bool = False, i_have_a_backup: bool = False,
                     out: Callable[[str], None] = print) -> Report:
    url = make_url(database_url.replace("postgres://", "postgresql://", 1))
    out(f"Target database: {url.render_as_string(hide_password=True)}")
    db_path = None
    if url.get_backend_name() == "sqlite":
        if not url.database or url.database == ":memory:":
            raise MergeError("An SQLite run needs a database file")
        db_path = Path(url.database).resolve()
        if not db_path.is_file():  # create_engine would silently create an empty database
            raise MergeError(f"Database file not found: {db_path}")
    elif apply and not i_have_a_backup:
        raise MergeError("Refusing --apply on a non-SQLite database without --i-have-a-backup. "
                         "Take a Neon branch / pg_dump first. Nothing was changed.")

    engine = create_engine(url.render_as_string(hide_password=False))
    try:
        inspector = inspect(engine)
        missing = [t for t in REQUIRED_TABLES if t not in inspector.get_table_names()]
        if missing:
            raise MergeError(f"Missing tables {missing}; is this the Pro Tracker database?")
        if "owner_id" not in {c["name"] for c in inspector.get_columns("foods")}:
            raise MergeError("foods.owner_id is missing; run tools/migrate_food_pools.py first")

        with engine.connect() as connection:
            groups = find_groups(connection)
        report = Report(merged=[g for g in groups if g.mergeable], conflicts=[g for g in groups if not g.mergeable])

        out("")
        out(f"Duplicate groups to merge ({len(report.merged)}):" if report.merged else "Duplicate groups to merge: none")
        for group in report.merged:
            _describe(group, out, action=True)
        out("")
        out(f"Calories or protein differ: NOT merged, decide yourself ({len(report.conflicts)}):"
            if report.conflicts else "Groups with different values: none")
        for group in report.conflicts:
            _describe(group, out, action=False)
        out("")

        if not apply:
            report.records_moved = sum(f.records for g in report.merged for f in g.extras)
            report.foods_deleted = sum(len(g.extras) for g in report.merged)
            out(f"Dry run: would move {report.records_moved} entr(ies) and delete {report.foods_deleted} food(s). "
                "Nothing was changed. Run again with --apply to merge.")
            return report
        if not report.merged:
            out("Nothing to merge.")
            return report

        if db_path is not None:
            out(f"Backup written and verified: {backup_sqlite(db_path)}")
        planned, report.merged = report.merged, []
        for group in planned:
            with engine.begin() as connection:  # one transaction per group: all or nothing
                result = _merge_group(connection, group)
            if result is None:
                report.skipped.append(group)
                out(f'  skipped "{group.keeper.name}" ({group.pool}): changed since the plan; run again')
                continue
            report.merged.append(group)
            report.records_moved += result[0]
            report.foods_deleted += result[1]
        out(f"Merged {len(report.merged)} group(s): moved {report.records_moved} entr(ies), "
            f"deleted {report.foods_deleted} food(s). Skipped: {len(report.skipped)}.")
        return report
    finally:
        engine.dispose()


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Merge duplicate foods (dry run unless --apply).")
    parser.add_argument("--database-url", required=True, help="e.g. sqlite:///tracker.db or postgresql://...")
    parser.add_argument("--apply", action="store_true", help="really merge (default: dry run, changes nothing)")
    parser.add_argument("--i-have-a-backup", action="store_true", help="required with --apply on PostgreSQL")
    args = parser.parse_args(argv)
    try:
        merge_duplicates(args.database_url, apply=args.apply, i_have_a_backup=args.i_have_a_backup)
    except MergeError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
