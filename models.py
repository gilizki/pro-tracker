"""Persistent domain models. These classes contain no request or UI logic."""
from sqlalchemy import Column, Date, Float, ForeignKey, Integer, String
from sqlalchemy.orm import relationship

from database import Base


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    username = Column(String(64), nullable=False, unique=True, index=True)
    password_hash = Column(String(256), nullable=False)
    records = relationship("UserRecord", back_populates="user", cascade="all, delete-orphan")
    sessions = relationship("AuthSession", back_populates="user", cascade="all, delete-orphan")


class Food(Base):
    """A global catalogue item shared by every authenticated user."""
    __tablename__ = "foods"

    id = Column(Integer, primary_key=True)
    name = Column(String(128), nullable=False, index=True)
    calories = Column(Float, nullable=False)  # per 100g
    protein = Column(Float, nullable=False)  # per 100g
    records = relationship("UserRecord", back_populates="food")


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
