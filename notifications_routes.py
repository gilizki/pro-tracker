"""HTTP routes and the meal-logging hook for push notifications.

``build_notifications_router`` receives main.py's existing ``current_user``
dependency, so authentication is the same as everywhere else.  It also serves
the service worker and the web app manifest from the site root, because a
service worker only controls pages inside its own folder ("/").
"""
import datetime as dt
import logging
from pathlib import Path
from typing import Callable

from fastapi import APIRouter, BackgroundTasks, Depends
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

import database
from database import get_db
from israel_time import israel_now, tracker_day  # noqa: F401  (israel_now is re-exported for main.py)
from models import User
import notifications_models  # noqa: F401  (registers the tables before create_all)
from notifications_repository import PushSubscriptionRepository
from notifications_schemas import SubscriptionCreate, SubscriptionRemove
from notifications_service import NotificationService

logger = logging.getLogger(__name__)
FRONTEND_DIR = Path(__file__).resolve().parent / "frontend"

notification_service = NotificationService()
subscriptions = PushSubscriptionRepository()


def schedule_meal_notifications(background_tasks: BackgroundTasks, user_id: int, record_date: dt.date,
                                logged_at: dt.datetime) -> None:
    """Called by POST /records/ only after the meal was saved. Meals for any day
    other than the current tracker day never trigger notifications."""
    if record_date != tracker_day(logged_at):
        return
    background_tasks.add_task(run_meal_notifications, user_id, record_date, logged_at)


def run_meal_notifications(user_id: int, record_date: dt.date, logged_at: dt.datetime) -> None:
    """Runs after the response was sent. Must never raise: meal logging is already done."""
    try:
        db = database.SessionLocal()
        try:
            notification_service.handle_meal(db, user_id, record_date, logged_at)
        finally:
            db.close()
    except Exception:
        logger.exception("Friend notifications failed")


def build_notifications_router(current_user: Callable[..., User]) -> APIRouter:
    router = APIRouter(tags=["notifications"])

    @router.get("/notifications/config")
    def config():
        """The VAPID public key, or null when push is not configured on this server."""
        return {"public_key": notification_service.sender.public_key}

    @router.post("/notifications/subscriptions")
    def subscribe(body: SubscriptionCreate, user: User = Depends(current_user), db: Session = Depends(get_db)):
        subscriptions.upsert(db, user.id, body.endpoint, body.keys.p256dh, body.keys.auth)
        return {"status": "subscribed"}

    @router.post("/notifications/unsubscribe")
    def unsubscribe(body: SubscriptionRemove, db: Session = Depends(get_db)):
        """No login needed (used during logout): knowing the secret endpoint proves it is this device."""
        subscriptions.delete_by_endpoint(db, body.endpoint)
        return {"status": "unsubscribed"}

    @router.get("/sw.js", include_in_schema=False)
    def service_worker():
        return FileResponse(FRONTEND_DIR / "sw.js", media_type="text/javascript",
                            headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"})

    @router.get("/manifest.webmanifest", include_in_schema=False)
    def manifest():
        return FileResponse(FRONTEND_DIR / "manifest.webmanifest", media_type="application/manifest+json")

    return router
