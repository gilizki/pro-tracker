"""Database configuration only; ORM models live in models.py."""
import os
from pathlib import Path

from sqlalchemy import create_engine
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


def get_db():
    """FastAPI dependency that scopes one SQLAlchemy session to a request."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
