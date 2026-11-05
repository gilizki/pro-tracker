"""Repository layer for push notifications. All its SQLAlchemy queries live here."""
import datetime as dt
from typing import Dict, Iterable, List

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.notifications.models import PushSubscription, SentNotification

RESERVED = "reserved"
DUPLICATE = "duplicate"      # this type was already sent today about this friend
CAP_REACHED = "cap_reached"  # the recipient already got DAILY_CAP notifications today


class PushSubscriptionRepository:
    def upsert(self, db: Session, user_id: int, endpoint: str, p256dh: str, auth: str) -> None:
        """Creates the device row, or moves it to ``user_id`` and refreshes its keys."""
        for _ in range(2):  # second round only if the same endpoint was inserted concurrently
            row = db.query(PushSubscription).filter(PushSubscription.endpoint == endpoint).first()
            if row is None:
                db.add(PushSubscription(user_id=user_id, endpoint=endpoint, p256dh=p256dh, auth=auth))
            else:
                row.user_id, row.p256dh, row.auth = user_id, p256dh, auth
            try:
                db.commit()
                return
            except IntegrityError:
                db.rollback()

    def delete_by_endpoint(self, db: Session, endpoint: str) -> None:
        db.query(PushSubscription).filter(PushSubscription.endpoint == endpoint).delete()
        db.commit()

    def for_users(self, db: Session, user_ids: Iterable[int]) -> Dict[int, List[PushSubscription]]:
        user_ids = list(user_ids)
        if not user_ids:
            return {}
        result: Dict[int, List[PushSubscription]] = {}
        rows = db.query(PushSubscription).filter(PushSubscription.user_id.in_(user_ids)).order_by(PushSubscription.id)
        for row in rows:
            result.setdefault(row.user_id, []).append(row)
        return result


class SentNotificationRepository:
    def exists(self, db: Session, recipient_id: int, friend_id: int, notification_type: str, day: dt.date) -> bool:
        return db.query(SentNotification.id).filter(
            SentNotification.recipient_id == recipient_id,
            SentNotification.friend_id == friend_id,
            SentNotification.type == notification_type,
            SentNotification.day == day,
        ).first() is not None

    def reserve(self, db: Session, recipient_id: int, friend_id: int, notification_type: str,
                day: dt.date, cap: int) -> str:
        """Atomically claims one of the recipient's daily slots for this notification.

        The database constraints decide: a concurrent duplicate or a full cap makes
        the insert fail, so this is safe across requests and server processes.
        """
        if self.exists(db, recipient_id, friend_id, notification_type, day):
            return DUPLICATE
        for slot in range(1, cap + 1):
            db.add(SentNotification(recipient_id=recipient_id, friend_id=friend_id,
                                    type=notification_type, day=day, slot=slot))
            try:
                db.commit()
                return RESERVED
            except IntegrityError:
                db.rollback()
                if self.exists(db, recipient_id, friend_id, notification_type, day):
                    return DUPLICATE  # reserved concurrently by another meal
                # otherwise this slot is taken: try the next one
        return CAP_REACHED
