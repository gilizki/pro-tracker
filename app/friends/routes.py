"""HTTP routes for the friends feature.

The router is built by ``build_friends_router`` and receives main.py's existing
``current_user`` dependency, so authentication is exactly the same as for every
other endpoint (no new or broadened auth rule).  Every route requires login.
"""
import datetime as dt
from typing import Callable, List

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.friends.schemas import FriendRequestCreate, FriendRequestResult, FriendStatus, InviteCode, PendingRequests
from app.friends.service import FriendService, resolve_friend_date
from app.tracker.models import User

friend_service = FriendService()

# Required on purpose: the server never guesses the day. The client computes it with
# frontend/day.js (02:00–01:59 Israel time), the single source of that rule.
DATE_HELP = "The caller's tracker day (YYYY-MM-DD, 02:00 Israel-time boundary); must be within 1 day of today's UTC date"


def build_friends_router(current_user: Callable[..., User]) -> APIRouter:
    router = APIRouter(prefix="/friends", tags=["friends"])

    @router.get("/invite-code", response_model=InviteCode)
    def get_invite_code(user: User = Depends(current_user), db: Session = Depends(get_db)):
        return {"code": friend_service.get_invite_code(db, user)}

    @router.post("/invite-code/regenerate", response_model=InviteCode)
    def regenerate_invite_code(user: User = Depends(current_user), db: Session = Depends(get_db)):
        return {"code": friend_service.regenerate_invite_code(db, user)}

    @router.post("/requests", response_model=FriendRequestResult)
    def send_request(body: FriendRequestCreate, user: User = Depends(current_user), db: Session = Depends(get_db)):
        return friend_service.send_request(db, user, body.username, body.invite_code)

    @router.get("/requests", response_model=PendingRequests)
    def list_requests(user: User = Depends(current_user), db: Session = Depends(get_db)):
        return friend_service.list_requests(db, user)

    @router.post("/requests/{request_id}/accept")
    def accept_request(request_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
        friend_service.accept_request(db, user, request_id)
        return {"status": "accepted"}

    @router.post("/requests/{request_id}/decline")
    def decline_request(request_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
        friend_service.decline_request(db, user, request_id)
        return {"status": "declined"}

    @router.delete("/requests/{request_id}")
    def cancel_request(request_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
        friend_service.cancel_request(db, user, request_id)
        return {"status": "cancelled"}

    @router.get("", response_model=List[FriendStatus])
    def list_friends(
        date: dt.date = Query(description=DATE_HELP),
        user: User = Depends(current_user),
        db: Session = Depends(get_db),
    ):
        return friend_service.list_friends(db, user, resolve_friend_date(date))

    @router.get("/{friend_id}/status", response_model=FriendStatus)
    def friend_status(
        friend_id: int,
        date: dt.date = Query(description=DATE_HELP),
        user: User = Depends(current_user),
        db: Session = Depends(get_db),
    ):
        return friend_service.friend_status(db, user, friend_id, resolve_friend_date(date))

    @router.delete("/{friend_id}")
    def remove_friend(friend_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
        friend_service.remove_friend(db, user, friend_id)
        return {"status": "removed"}

    return router
