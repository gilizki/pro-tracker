"""Business rules for the friends feature.

Privacy rules enforced here (server side):
  * Another user's nutrition data is returned only when an ACCEPTED friendship
    exists between the caller and that user.
  * Only daily totals are returned.  Totals come from the existing
    ``RecordService.summary`` (reused, not modified, so the numbers can never
    diverge from the user's own view); its ``meals`` list is discarded here.
  * Non-friends, pending requests and non-existent users all produce the same
    404, so the status endpoints reveal nothing about other users.
  * The caller must send their tracker day (02:00 Israel-time boundary, from
    frontend/day.js); only a date within one day of the server's UTC date is
    accepted, so a friend sees "today" but cannot browse history.
"""
import datetime as dt
import secrets
from typing import List, Optional

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.friends.models import STATUS_ACCEPTED, STATUS_PENDING
from app.friends.repository import FriendshipRepository, InviteCodeRepository
from app.tracker.models import User
from app.tracker.repositories import UserRepository
from app.tracker.services import RecordService

INVITE_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O, 1/I look-alikes
INVITE_CODE_LENGTH = 8
FRIEND_NOT_FOUND = "Friend not found"
REQUEST_NOT_FOUND = "Request not found"


def _not_found(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=detail)


def format_invite_code(code: str) -> str:
    return f"{code[:4]}-{code[4:]}"


def normalize_invite_code(raw: str) -> Optional[str]:
    """Upper-cases and strips dashes/spaces. Returns None for impossible codes."""
    code = "".join(ch for ch in raw.upper() if ch not in "- ")
    if len(code) != INVITE_CODE_LENGTH or any(ch not in INVITE_CODE_ALPHABET for ch in code):
        return None
    return code


def _utc_today() -> dt.date:
    return dt.datetime.now(dt.timezone.utc).date()


def resolve_friend_date(requested: dt.date) -> dt.date:
    """The caller's tracker day (computed by frontend/day.js), within 1 day of the UTC date.

    Israel is UTC+2/+3, so with the 02:00 boundary the tracker day is always the UTC
    date or the day after it; the +-1 window covers that while blocking history browsing.
    """
    utc_today = _utc_today()
    if abs((requested - utc_today).days) > 1:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Only today's status is available")
    return requested


class FriendService:
    def __init__(self):
        self.friendships = FriendshipRepository()
        self.codes = InviteCodeRepository()
        self.users = UserRepository()
        self.records = RecordService()

    # ----- invite codes -------------------------------------------------
    def get_invite_code(self, db: Session, user: User) -> str:
        row = self.codes.find_for_user(db, user.id)
        if row is not None:
            return format_invite_code(row.code)
        return self._store_new_code(db, user)

    def regenerate_invite_code(self, db: Session, user: User) -> str:
        return self._store_new_code(db, user)

    def _store_new_code(self, db: Session, user: User) -> str:
        for _ in range(10):
            code = "".join(secrets.choice(INVITE_CODE_ALPHABET) for _ in range(INVITE_CODE_LENGTH))
            if self.codes.find_by_code(db, code) is None and self.codes.save(db, user.id, code):
                return format_invite_code(code)
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Could not create invite code, try again")

    # ----- requests -----------------------------------------------------
    def send_request(self, db: Session, user: User, username: Optional[str], invite_code: Optional[str]) -> dict:
        target = self._resolve_target(db, username, invite_code)
        if target.id == user.id:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="You cannot add yourself")

        existing = self.friendships.find_between(db, user.id, target.id)
        if existing is None:
            if self.friendships.create_request(db, user.id, target.id) is not None:
                return {"status": STATUS_PENDING, "username": target.username}
            existing = self.friendships.find_between(db, user.id, target.id)  # created concurrently
            if existing is None:
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Please try again")

        if existing.status == STATUS_ACCEPTED:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="You are already friends")
        if existing.requester_id == user.id:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Friend request already sent")
        # The target had already asked us: sending back counts as accepting.
        self.friendships.accept(db, existing)
        return {"status": STATUS_ACCEPTED, "username": target.username}

    def _resolve_target(self, db: Session, username: Optional[str], invite_code: Optional[str]) -> User:
        if username is not None:
            target = self.users.find_by_username(db, username.strip())
            if target is None:
                raise _not_found("User not found")
            return target
        code = normalize_invite_code(invite_code or "")
        row = self.codes.find_by_code(db, code) if code else None
        target = db.get(User, row.user_id) if row else None
        if target is None:
            raise _not_found("Invalid invite code")
        return target

    def list_requests(self, db: Session, user: User) -> dict:
        return {
            "incoming": [{"id": f.id, "username": u.username} for f, u in self.friendships.pending_incoming(db, user.id)],
            "outgoing": [{"id": f.id, "username": u.username} for f, u in self.friendships.pending_outgoing(db, user.id)],
        }

    def accept_request(self, db: Session, user: User, request_id: int) -> None:
        self.friendships.accept(db, self._incoming_pending(db, user, request_id))

    def decline_request(self, db: Session, user: User, request_id: int) -> None:
        self.friendships.delete(db, self._incoming_pending(db, user, request_id))

    def cancel_request(self, db: Session, user: User, request_id: int) -> None:
        friendship = self.friendships.find_by_id(db, request_id)
        if friendship is None or friendship.status != STATUS_PENDING or friendship.requester_id != user.id:
            raise _not_found(REQUEST_NOT_FOUND)
        self.friendships.delete(db, friendship)

    def _incoming_pending(self, db: Session, user: User, request_id: int):
        friendship = self.friendships.find_by_id(db, request_id)
        if friendship is None or friendship.status != STATUS_PENDING or friendship.addressee_id != user.id:
            raise _not_found(REQUEST_NOT_FOUND)
        return friendship

    # ----- friends ------------------------------------------------------
    def remove_friend(self, db: Session, user: User, friend_id: int) -> None:
        friendship = self.friendships.find_between(db, user.id, friend_id)
        if friendship is None or friendship.status != STATUS_ACCEPTED:
            raise _not_found(FRIEND_NOT_FOUND)
        self.friendships.delete(db, friendship)

    def list_friends(self, db: Session, user: User, day: dt.date) -> List[dict]:
        return [self._daily_totals(db, friend, day) for friend in self.friendships.accepted_friends(db, user.id)]

    def friend_status(self, db: Session, user: User, friend_id: int, day: dt.date) -> dict:
        # The friendship check comes first; nothing about the target is read before it.
        if friend_id == user.id or not self.friendships.are_friends(db, user.id, friend_id):
            raise _not_found(FRIEND_NOT_FOUND)
        friend = db.get(User, friend_id)
        if friend is None:
            raise _not_found(FRIEND_NOT_FOUND)
        return self._daily_totals(db, friend, day)

    def _daily_totals(self, db: Session, friend: User, day: dt.date) -> dict:
        """Totals only. Built from an allow-list; the summary's ``meals`` are dropped."""
        summary = self.records.summary(db, friend, day)
        return {
            "user_id": friend.id,
            "username": friend.username,
            "date": summary["date"],
            "total_calories": summary["total_calories"],
            "total_protein": summary["total_protein"],
        }
