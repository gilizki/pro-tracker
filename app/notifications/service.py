"""Business rules for friend push notifications.

Runs AFTER a meal was saved, in a background task with its own DB session:
  * Day totals come from the existing ``RecordService.summary`` (which already
    includes the new meal), never from a parallel calculation.
  * Only the meal's own tracker day counts (checked by the caller and again here).
  * Recipients are the friend's ACCEPTED friends at this moment, so a removed
    friendship stops notifications immediately.
  * Each recipient gets each type at most once per tracker day (whichever friend
    triggered it) and at most one notification per meal: the first applicable
    type in ``PRIORITY`` not yet sent to them today. Enforced by a database
    constraint (see app/notifications/models.py), so it holds under concurrency.
"""
import datetime as dt
import logging
from decimal import ROUND_HALF_UP, Decimal
from typing import List, Optional

from sqlalchemy.orm import Session

from app.friends.repository import FriendshipRepository
from app.core.israel_time import DAY_START_HOUR, TIME_ZONE, tracker_day
from app.tracker.models import User, UserRecord
from app.notifications.config import (
    BEFORE_TEN, BEFORE_TEN_BEFORE_HOUR, BULLDOZER, BULLDOZER_BEFORE_HOUR, BULLDOZER_KCAL, EARLY_BIRD,
    EARLY_BIRD_BEFORE_HOUR, MEAL_PROTEIN, MEAL_PROTEIN_GRAMS, MESSAGES, MIDNIGHT_MUNCH, MIDNIGHT_MUNCH_UNTIL_HOUR,
    PRIORITY, PROTEIN_MACHINE, PROTEIN_MACHINE_GRAMS, PUSH_TTL_SECONDS,
)
from app.notifications.push import GONE, WebPushSender
from app.notifications.repository import PushSubscriptionRepository, SentNotificationRepository
from app.tracker.services import RecordService

logger = logging.getLogger(__name__)


def meal_protein_grams(record: UserRecord) -> float:
    """Protein in this one logged meal: grams eaten * protein per 100 g / 100 (unrounded)."""
    return record.amount_grams * record.food.protein / 100


def format_grams(grams: float) -> str:
    """One decimal, rounded half up; whole numbers without ".0" (25.1 -> "25.1", 30.0 -> "30")."""
    rounded = Decimal(repr(grams)).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
    return str(rounded.to_integral_value()) if rounded == rounded.to_integral_value() else str(rounded)


def fired_types(total_protein: float, total_calories: float, logged_at: dt.datetime,
                meal_protein: Optional[float] = None) -> List[str]:
    """The notification types this meal triggers, in ``PRIORITY`` order.

    ``meal_protein`` is the logged meal's own protein, or None when unknown.
    """
    hour = logged_at.astimezone(TIME_ZONE).hour
    fired = set()
    if hour < MIDNIGHT_MUNCH_UNTIL_HOUR:
        fired.add(MIDNIGHT_MUNCH)
    if DAY_START_HOUR <= hour < EARLY_BIRD_BEFORE_HOUR:
        fired.add(EARLY_BIRD)
    if DAY_START_HOUR <= hour < BEFORE_TEN_BEFORE_HOUR:
        fired.add(BEFORE_TEN)
    if meal_protein is not None and meal_protein > MEAL_PROTEIN_GRAMS:
        fired.add(MEAL_PROTEIN)
    if total_calories >= BULLDOZER_KCAL and DAY_START_HOUR <= hour < BULLDOZER_BEFORE_HOUR:
        fired.add(BULLDOZER)
    if total_protein >= PROTEIN_MACHINE_GRAMS:
        fired.add(PROTEIN_MACHINE)
    return [t for t in PRIORITY if t in fired]


def build_payload(notification_type: str, friend_id: int, friend_username: str,
                  meal_protein: Optional[float] = None) -> dict:
    message = MESSAGES[notification_type]
    protein = format_grams(meal_protein) if meal_protein is not None else ""
    return {
        "type": notification_type,
        "title": message["title"],
        "body": message["body"].format(name=friend_username, protein=protein),
        "url": message["url"],
        "silent": message["silent"],
        "tag": f"{notification_type}-{friend_id}",
    }


class NotificationService:
    def __init__(self, sender=None):
        self.records = RecordService()
        self.friendships = FriendshipRepository()
        self.subscriptions = PushSubscriptionRepository()
        self.sent = SentNotificationRepository()
        self.sender = sender or WebPushSender()

    def handle_meal(self, db: Session, friend_id: int, record_date: dt.date, logged_at: dt.datetime,
                    record_id: Optional[int] = None) -> None:
        if record_date != tracker_day(logged_at) or not self.sender.enabled:
            return
        friend = db.get(User, friend_id)
        if friend is None:
            return
        summary = self.records.summary(db, friend, record_date)
        record = db.get(UserRecord, record_id) if record_id is not None else None
        # A meal deleted before this task ran simply has no MEAL_PROTEIN; the other types still apply.
        meal_protein = meal_protein_grams(record) if record is not None and record.user_id == friend_id else None
        fired = fired_types(summary["total_protein"], summary["total_calories"], logged_at, meal_protein)
        if not fired:
            return  # the common case: no notification at all

        friend_username = friend.username
        recipient_ids = [r.id for r in self.friendships.accepted_friends(db, friend_id)]
        devices_by_user = {
            user_id: [(d.endpoint, d.p256dh, d.auth) for d in devices]
            for user_id, devices in self.subscriptions.for_users(db, recipient_ids).items()
        }
        for recipient_id in recipient_ids:
            devices = devices_by_user.get(recipient_id)
            if not devices:
                continue  # no device: nothing is marked as sent
            try:
                notification_type = self._reserve_one(db, recipient_id, friend_id, record_date, fired)
                if notification_type:
                    self._deliver(db, devices, build_payload(notification_type, friend_id, friend_username, meal_protein))
            except Exception:
                db.rollback()
                logger.exception("Notification for one recipient failed")

    def _reserve_one(self, db: Session, recipient_id: int, friend_id: int, day: dt.date, fired: List[str]):
        """The first fired type this recipient has not received today; None if all were already sent."""
        for notification_type in fired:
            if self.sent.reserve(db, recipient_id, friend_id, notification_type, day):
                return notification_type
        return None

    def _deliver(self, db: Session, devices, payload: dict) -> None:
        for endpoint, p256dh, auth in devices:
            try:
                if self.sender.send(endpoint, p256dh, auth, payload, PUSH_TTL_SECONDS) == GONE:
                    self.subscriptions.delete_by_endpoint(db, endpoint)
            except Exception:
                db.rollback()
                logger.exception("Push to one device failed")
