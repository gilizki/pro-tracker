"""Friends feature tables. Additive only: no existing table or model is changed.

Plain foreign keys are used (no relationships added to ``User``) so models.py
stays untouched.  Importing this module registers the tables with ``Base`` so
the existing ``create_all`` call creates them on startup.
"""
from datetime import datetime

from sqlalchemy import CheckConstraint, Column, DateTime, ForeignKey, Integer, String

from database import Base

STATUS_PENDING = "pending"
STATUS_ACCEPTED = "accepted"


class Friendship(Base):
    """A friend request (pending) or friendship (accepted) between two users.

    ``pair_key`` is "<smaller id>:<larger id>" and is unique, so at most one row
    can exist per pair of users regardless of who sent the request.
    """
    __tablename__ = "friendships"

    id = Column(Integer, primary_key=True)
    requester_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    addressee_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    status = Column(String(16), nullable=False, default=STATUS_PENDING)
    pair_key = Column(String(64), nullable=False, unique=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    __table_args__ = (
        CheckConstraint("requester_id <> addressee_id", name="ck_friendships_not_self"),
        CheckConstraint("status IN ('pending', 'accepted')", name="ck_friendships_status"),
    )


class FriendInviteCode(Base):
    """One permanent invite code per user, replaced only when regenerated."""
    __tablename__ = "friend_invite_codes"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, unique=True)
    code = Column(String(16), nullable=False, unique=True)
