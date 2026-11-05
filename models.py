"""Persistent domain models. These classes contain no request or UI logic."""
from sqlalchemy import Boolean, CheckConstraint, Column, Date, Float, ForeignKey, Integer, String, false
from sqlalchemy.orm import relationship

from database import Base

# Food.suggestion_status values. NULL means "not suggested".
SUGGESTION_PENDING = "pending"
SUGGESTION_REJECTED = "rejected"


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    username = Column(String(64), nullable=False, unique=True, index=True)
    password_hash = Column(String(256), nullable=False)
    is_admin = Column(Boolean, nullable=False, default=False, server_default=false())
    records = relationship("UserRecord", back_populates="user", cascade="all, delete-orphan")
    sessions = relationship("AuthSession", back_populates="user", cascade="all, delete-orphan")


class Food(Base):
    """A catalogue item. Global (owner_id NULL, visible to everyone, admin-managed)
    or personal (owner_id set, visible only to its owner).

    A personal food may be suggested for the global pool (suggestion_status
    'pending'); approving it clears owner_id in place so the id never changes.
    """
    __tablename__ = "foods"

    id = Column(Integer, primary_key=True)
    name = Column(String(128), nullable=False, index=True)
    calories = Column(Float, nullable=False)  # per 100g
    protein = Column(Float, nullable=False)  # per 100g
    owner_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    suggestion_status = Column(String(16), nullable=True)
    records = relationship("UserRecord", back_populates="food")
    owner = relationship("User")

    __table_args__ = (
        CheckConstraint("suggestion_status IS NULL OR suggestion_status IN ('pending', 'rejected')",
                        name="ck_foods_suggestion_status"),
    )


class UserRecord(Base):
    """One user's private meal entry, linked to a shared Food."""
    __tablename__ = "user_records"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    food_id = Column(Integer, ForeignKey("foods.id"), nullable=False, index=True)
    amount_grams = Column(Float, nullable=False)
    record_date = Column(Date, nullable=False, index=True)
    user = relationship("User", back_populates="records")
    food = relationship("Food", back_populates="records")


class AuthSession(Base):
    """A persistent, revocable login session. Only a token digest is stored."""
    __tablename__ = "auth_sessions"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    token_hash = Column(String(64), nullable=False, unique=True, index=True)
    user = relationship("User", back_populates="sessions")
