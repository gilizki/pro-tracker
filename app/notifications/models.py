"""Push-notification tables. Additive only: no existing table or model is changed.

Importing this module registers the tables with ``Base`` so the existing
``create_all`` call creates them on startup.
"""
from datetime import datetime, timezone

from sqlalchemy import CheckConstraint, Column, Date, DateTime, ForeignKey, Integer, String, UniqueConstraint

from app.core.database import Base
from app.notifications.config import ALL_TYPES


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

    * ``uq_sent_once_per_day``: each type at most once per day per friend per recipient.
    * ``uq_sent_daily_slot``: the sender only uses slots 1..DAILY_CAP, and two rows can
      never share a slot, so a recipient can never get more than DAILY_CAP per day,
      even when two meals are logged at the same moment.
    """
    __tablename__ = "sent_notifications"

    id = Column(Integer, primary_key=True)
    recipient_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    friend_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    type = Column(String(32), nullable=False)
    day = Column(Date, nullable=False)
    slot = Column(Integer, nullable=False)
    created_at = Column(DateTime, nullable=False, default=utc_now)

    __table_args__ = (
        UniqueConstraint("recipient_id", "friend_id", "type", "day", name="uq_sent_once_per_day"),
        UniqueConstraint("recipient_id", "day", "slot", name="uq_sent_daily_slot"),
        CheckConstraint("recipient_id <> friend_id", name="ck_sent_not_self"),
        CheckConstraint("slot >= 1", name="ck_sent_slot_positive"),
        CheckConstraint("type IN ({})".format(", ".join(f"'{t}'" for t in ALL_TYPES)), name="ck_sent_type"),
    )
