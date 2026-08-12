"""Database configuration only; ORM models live in models.py."""
import os
from pathlib import Path

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import declarative_base, sessionmaker


# Resolve the default database relative to this source file, rather than the
# process working directory.  This means running Uvicorn from another folder
# still opens the same application database.
DEFAULT_DATABASE_PATH = Path(__file__).resolve().parent / "tracker.db"
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
            from security import PasswordHasher

            legacy_users = connection.execute(
                text("SELECT id, password FROM users WHERE password IS NOT NULL AND password_hash IS NULL")
            ).mappings()
            for user in legacy_users:
                connection.execute(
                    text("UPDATE users SET password_hash = :password_hash WHERE id = :id"),
                    {"id": user["id"], "password_hash": PasswordHasher.hash(user["password"])},
                )


def get_db():
    """FastAPI dependency that scopes one SQLAlchemy session to a request."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
