"""Business rules for friend push notifications.

Runs AFTER a meal was saved, in a background task with its own DB session:
  * Totals come from the existing ``RecordService.summary`` (which already
    includes the new meal), never from a parallel calculation.
  * Only the meal's own tracker day counts (checked by the caller and again here).
  * Recipients are the friend's ACCEPTED friends at this moment, so a removed
    friendship stops notifications immediately.
  * At most one notification per meal per recipient (``PRIORITY``), each type
    at most once per day per friend, and at most ``DAILY_CAP`` per recipient
    per day, all enforced by database constraints (see notifications_models.py).
"""
import datetime as dt
import logging
from typing import List

from sqlalchemy.orm import Session

from friends_repository import FriendshipRepository
from israel_time import TIME_ZONE, tracker_day
from models import User
from notifications_config import (
    BULLDOZER, BULLDOZER_BEFORE_HOUR, BULLDOZER_KCAL, DAILY_CAP, MESSAGES, MIDNIGHT_MUNCH,
    MIDNIGHT_MUNCH_UNTIL_HOUR, PRIORITY, PROTEIN_MACHINE, PROTEIN_MACHINE_GRAMS, PUSH_TTL_SECONDS,
)
from notifications_push import GONE, WebPushSender
from notifications_repository import CAP_REACHED, RESERVED, PushSubscriptionRepository, SentNotificationRepository
from services import RecordService

logger = logging.getLogger(__name__)


def fired_types(total_protein: float, total_calories: float, logged_at: dt.datetime) -> List[str]:
    """The notification types this meal triggers, in ``PRIORITY`` order."""
    hour = logged_at.astimezone(TIME_ZONE).hour
    fired = set()
    if hour < MIDNIGHT_MUNCH_UNTIL_HOUR:
        fired.add(MIDNIGHT_MUNCH)
    if total_calories >= BULLDOZER_KCAL and MIDNIGHT_MUNCH_UNTIL_HOUR <= hour < BULLDOZER_BEFORE_HOUR:
        fired.add(BULLDOZER)
    if total_protein >= PROTEIN_MACHINE_GRAMS:
        fired.add(PROTEIN_MACHINE)
    return [t for t in PRIORITY if t in fired]


def build_payload(notification_type: str, friend_id: int, friend_username: str) -> dict:
    message = MESSAGES[notification_type]
    return {
        "type": notification_type,
        "title": message["title"],
        "body": message["body"].format(name=friend_username),
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

    def handle_meal(self, db: Session, friend_id: int, record_date: dt.date, logged_at: dt.datetime) -> None:
        if record_date != tracker_day(logged_at) or not self.sender.enabled:
            return
        friend = db.get(User, friend_id)
        if friend is None:
            return
        summary = self.records.summary(db, friend, record_date)
        fired = fired_types(summary["total_protein"], summary["total_calories"], logged_at)
        if not fired:
            return  # the common case: one summary query and done

        friend_username = friend.username
        recipient_ids = [r.id for r in self.friendships.accepted_friends(db, friend_id)]
        devices_by_user = {
            user_id: [(d.endpoint, d.p256dh, d.auth) for d in devices]
            for user_id, devices in self.subscriptions.for_users(db, recipient_ids).items()
        }
        for recipient_id in recipient_ids:
            devices = devices_by_user.get(recipient_id)
            if not devices:
                continue  # no device: nothing reserved, the cap is not used up
            try:
                notification_type = self._reserve_one(db, recipient_id, friend_id, record_date, fired)
                if notification_type:
                    self._deliver(db, devices, build_payload(notification_type, friend_id, friend_username))
            except Exception:
                db.rollback()
                logger.exception("Notification for one recipient failed")

    def _reserve_one(self, db: Session, recipient_id: int, friend_id: int, day: dt.date, fired: List[str]):
        """The first fired type not yet sent today about this friend; the others are not reserved."""
        for notification_type in fired:
            outcome = self.sent.reserve(db, recipient_id, friend_id, notification_type, day, DAILY_CAP)
            if outcome == RESERVED:
                return notification_type
            if outcome == CAP_REACHED:
                return None
        return None

    def _deliver(self, db: Session, devices, payload: dict) -> None:
        for endpoint, p256dh, auth in devices:
            try:
                if self.sender.send(endpoint, p256dh, auth, payload, PUSH_TTL_SECONDS) == GONE:
                    self.subscriptions.delete_by_endpoint(db, endpoint)
            except Exception:
                db.rollback()
                logger.exception("Push to one device failed")
