"""Database configuration only; ORM models live in app/tracker/models.py and the feature packages."""
import os
from pathlib import Path

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import declarative_base, sessionmaker


# Resolve the default database relative to this source file, rather than the
# process working directory.  This means running Uvicorn from another folder
# still opens the same application database.
DEFAULT_DATABASE_PATH = Path(__file__).resolve().parents[2] / "tracker.db"  # project root
DATABASE_URL = os.getenv("DATABASE_URL", f"sqlite:///{DEFAULT_DATABASE_PATH.as_posix()}")
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

engine_options = {"connect_args": {"check_same_thread": False}} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, **engine_options)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def migrate_legacy_user_passwords() -> None:
    """Upgrade databases created before password hashes were introduced.

    ``create_all`` creates missing tables but deliberately does not alter existing
    ones.  The first deployed version stored a plaintext ``users.password``
    column, while the current login flow uses ``password_hash``.  Without this
    migration, logging in against that existing database raises a server error.
    """
    inspector = inspect(engine)
    if "users" not in inspector.get_table_names():
        return

    column_names = {column["name"] for column in inspector.get_columns("users")}
    # The column name and type are fixed here; no user input reaches this SQL.
    with engine.begin() as connection:
        if "password_hash" not in column_names:
            connection.execute(text("ALTER TABLE users ADD COLUMN password_hash VARCHAR(256)"))

        # Preserve accounts from the old schema by hashing their existing
        # passwords immediately.  Importing here keeps the database module
        # independent of authentication services during normal startup.
        if "password" in column_names:
            from app.core.security import PasswordHasher

            legacy_users = connection.execute(
                text("SELECT id, password FROM users WHERE password IS NOT NULL AND password_hash IS NULL")
            ).mappings()
            for user in legacy_users:
                connection.execute(
                    text("UPDATE users SET password_hash = :password_hash WHERE id = :id"),
                    {"id": user["id"], "password_hash": PasswordHasher.hash(user["password"])},
                )


FOOD_POOLS_COLUMNS = {"users": {"is_admin"}, "foods": {"owner_id", "suggestion_status"}}


def require_food_pools_schema() -> None:
    """Fail fast if the database predates the global/personal food pools.

    This only checks; it never alters the database.  The migration is a separate,
    reviewable script: tools/migrate_food_pools.py.
    """
    inspector = inspect(engine)
    missing = [
        f"{table}.{column}"
        for table, columns in FOOD_POOLS_COLUMNS.items()
        for column in sorted(columns - {c["name"] for c in inspector.get_columns(table)})
    ]
    if missing:
        raise RuntimeError(
            f"Database schema is out of date (missing {', '.join(missing)}). "
            "Back it up, then run: python tools/migrate_food_pools.py --admin <username>"
        )


def require_sent_notifications_schema() -> None:
    """Fail fast if sent_notifications still has the old max-2-per-day shape.

    This only checks; it never alters the database.  The migration is a separate,
    reviewable script: tools/migrate_sent_notifications.py.
    """
    from app.notifications.models import outdated_sent_notifications_schema  # lazy: that module imports Base from here

    problems = outdated_sent_notifications_schema(inspect(engine))
    if problems:
        raise RuntimeError(
            f"Database schema is out of date (sent_notifications: {', '.join(problems)}). "
            "Back it up, then run: python tools/migrate_sent_notifications.py"
        )


def get_db():
    """FastAPI dependency that scopes one SQLAlchemy session to a request."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
