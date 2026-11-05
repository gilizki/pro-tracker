"""Push-notification tables.

Importing this module registers the tables with ``Base`` so the existing
``create_all`` call creates them in a NEW database. An existing database is never
altered at startup: see tools/migrate_sent_notifications.py.
"""
from datetime import datetime, timezone
from typing import List

from sqlalchemy import CheckConstraint, Column, Date, DateTime, ForeignKey, Integer, String, UniqueConstraint

from app.core.database import Base

TYPE_PER_DAY_CONSTRAINT = "uq_sent_type_per_day"
# Constraints of the old shape (max 2 per day, once per type per FRIEND per day, fixed type list).
OLD_CONSTRAINTS = {"uq_sent_once_per_day", "uq_sent_daily_slot", "ck_sent_slot_positive", "ck_sent_type"}


def utc_now() -> datetime:
    """Current UTC time, stored naive (same format as friends_models.created_at).
    Replaces datetime.utcnow(), which is deprecated since Python 3.12."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class PushSubscription(Base):
    """One subscribed device/browser. A user can have several."""
    __tablename__ = "push_subscriptions"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    endpoint = Column(String(1024), nullable=False, unique=True)
    p256dh = Column(String(256), nullable=False)
    auth = Column(String(64), nullable=False)
    created_at = Column(DateTime, nullable=False, default=utc_now)


class SentNotification(Base):
    """A notification sent (reserved before sending). Never shown in the app.

    ``uq_sent_type_per_day``: each type at most once per tracker day per recipient,
    whichever friend triggered it. The sender inserts the row BEFORE pushing, so two
    meals logged at the same moment can never both send the same type: the database
    lets only one insert win. ``friend_id`` is kept for information only.

    There is deliberately no CHECK on ``type``: the valid types live in
    app/notifications/config.py, so adding one needs no database change.
    Databases created before this shape are upgraded by tools/migrate_sent_notifications.py.
    """
    __tablename__ = "sent_notifications"

    id = Column(Integer, primary_key=True)
    recipient_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    friend_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    type = Column(String(32), nullable=False)
    day = Column(Date, nullable=False)
    created_at = Column(DateTime, nullable=False, default=utc_now)

    __table_args__ = (
        UniqueConstraint("recipient_id", "type", "day", name=TYPE_PER_DAY_CONSTRAINT),
        CheckConstraint("recipient_id <> friend_id", name="ck_sent_not_self"),
    )


def outdated_sent_notifications_schema(inspector) -> List[str]:
    """What is still in the old (max-2-per-day) shape; empty when up to date or the table is missing.

    Shared by the startup check (app/core/database.py) and tools/migrate_sent_notifications.py.
    """
    if SentNotification.__tablename__ not in inspector.get_table_names():
        return []
    table = SentNotification.__tablename__
    columns = {c["name"] for c in inspector.get_columns(table)}
    uniques = {u["name"] for u in inspector.get_unique_constraints(table)}
    checks = {c["name"] for c in inspector.get_check_constraints(table)}
    problems = []
    if "slot" in columns:
        problems.append("column slot")
    problems += [f"constraint {name}" for name in sorted(OLD_CONSTRAINTS & (uniques | checks))]
    if TYPE_PER_DAY_CONSTRAINT not in uniques:
        problems.append(f"missing constraint {TYPE_PER_DAY_CONSTRAINT}")
    return problems
