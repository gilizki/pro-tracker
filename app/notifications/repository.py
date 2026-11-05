"""Repository layer for push notifications. All its SQLAlchemy queries live here."""
import datetime as dt
from typing import Dict, Iterable, List

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.notifications.models import PushSubscription, SentNotification


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
    def reserve(self, db: Session, recipient_id: int, friend_id: int, notification_type: str, day: dt.date) -> bool:
        """Atomically claims ``notification_type`` for this recipient on this tracker day.

        True: this caller may send it. False: it was already sent today (or is being
        sent right now by a concurrent meal). The unique constraint decides, so this
        is safe across requests and server processes.
        """
        db.add(SentNotification(recipient_id=recipient_id, friend_id=friend_id, type=notification_type, day=day))
        try:
            db.commit()
            return True
        except IntegrityError:
            db.rollback()
            return False
