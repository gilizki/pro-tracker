"""Repository/DAO layer. All SQLAlchemy queries are contained here."""
from __future__ import annotations

from datetime import date
from typing import Optional

from sqlalchemy import distinct, func, or_
from sqlalchemy.orm import Session, joinedload

from app.tracker.models import SUGGESTION_PENDING, SUGGESTION_REJECTED, AuthSession, Food, User, UserRecord


def normalize_food_name(name: str) -> str:
    """Comparison key for duplicate detection: case- and whitespace-insensitive."""
    return " ".join(name.split()).casefold()


class UserRepository:
    def find_by_username(self, db: Session, username: str) -> Optional[User]:
        return db.query(User).filter(User.username == username).first()

    def create(self, db: Session, username: str, password_hash: str) -> User:
        user = User(username=username, password_hash=password_hash)
        db.add(user)
        db.commit()
        db.refresh(user)
        return user


class SessionRepository:
    def create(self, db: Session, user_id: int, token_hash: str) -> AuthSession:
        session = AuthSession(user_id=user_id, token_hash=token_hash)
        db.add(session)
        db.commit()
        return session

    def user_for_token(self, db: Session, token_hash: str) -> Optional[User]:
        session = db.query(AuthSession).options(joinedload(AuthSession.user)).filter(AuthSession.token_hash == token_hash).first()
        return session.user if session else None

    def delete(self, db: Session, token_hash: str) -> None:
        db.query(AuthSession).filter(AuthSession.token_hash == token_hash).delete()
        db.commit()


class FoodRepository:
    """Every read that serves a user is scoped by ``_visible_to``: global foods
    plus that user's own personal foods. Other users' personal foods are never
    returned, so callers cannot accidentally leak or modify them."""

    @staticmethod
    def _visible_to(user_id: int):
        return or_(Food.owner_id.is_(None), Food.owner_id == user_id)

    def list_visible(self, db: Session, user_id: int) -> list[Food]:
        return db.query(Food).filter(self._visible_to(user_id)).order_by(Food.name, Food.id).all()

    def find_visible(self, db: Session, food_id: int, user_id: int) -> Optional[Food]:
        return db.query(Food).filter(Food.id == food_id, self._visible_to(user_id)).first()

    def find_global_by_name(self, db: Session, name: str, exclude_id: Optional[int] = None) -> Optional[Food]:
        # Compared in Python: SQL lower() differs between SQLite and PostgreSQL for
        # non-ASCII text, and the global pool is small.
        key = normalize_food_name(name)
        for food in db.query(Food).filter(Food.owner_id.is_(None)).order_by(Food.id):
            if food.id != exclude_id and normalize_food_name(food.name) == key:
                return food
        return None

    def find_personal_by_name(self, db: Session, owner_id: int, name: str,
                              exclude_id: Optional[int] = None) -> Optional[Food]:
        key = normalize_food_name(name)
        for food in db.query(Food).filter(Food.owner_id == owner_id).order_by(Food.id):
            if food.id != exclude_id and normalize_food_name(food.name) == key:
                return food
        return None

    def list_pending(self, db: Session) -> list[Food]:
        return (
            db.query(Food)
            .options(joinedload(Food.owner))
            .filter(Food.owner_id.isnot(None), Food.suggestion_status == SUGGESTION_PENDING)
            .order_by(Food.id)
            .all()
        )

    def find_pending(self, db: Session, food_id: int) -> Optional[Food]:
        return (
            db.query(Food)
            .filter(Food.id == food_id, Food.owner_id.isnot(None), Food.suggestion_status == SUGGESTION_PENDING)
            .first()
        )

    def usage(self, db: Session, food_id: int) -> tuple[int, int]:
        """(distinct users, records) that reference this food."""
        users, records = (db.query(func.count(distinct(UserRecord.user_id)), func.count(UserRecord.id))
                          .filter(UserRecord.food_id == food_id).one())
        return users, records

    def delete_with_records(self, db: Session, food: Food, expected_records: int) -> Optional[int]:
        """Deletes the food and every record (any user) that references it, in ONE transaction.

        The confirmed count is checked against the DELETE's own row count, not a separate SELECT, so it is
        exact on both databases (Python's sqlite3 driver opens its transaction only at the first write, so
        on SQLite an earlier SELECT would not be part of it). If it is not ``expected_records`` (someone
        logged or removed it since the caller's preview), the deletion is rolled back and None is returned.
        The food row is locked first (PostgreSQL; SQLite ignores FOR UPDATE) so a concurrent INSERT into
        user_records waits instead of failing on the foreign key. Any failure rolls everything back; the
        only commit is the one below. Returns the number of records deleted."""
        try:
            locked = db.query(Food).filter(Food.id == food.id).with_for_update().first()
            if locked is None:
                db.rollback()
                return 0
            records = db.query(UserRecord).filter(UserRecord.food_id == food.id).delete(synchronize_session=False)
            if records != expected_records:
                db.rollback()
                return None
            # Bulk delete: db.delete(food) would try to null the FK of records still loaded in the session.
            db.query(Food).filter(Food.id == food.id).delete(synchronize_session=False)
            db.commit()
        except Exception:
            db.rollback()
            raise
        return records

    def create(self, db: Session, name: str, calories: float, protein: float,
               owner_id: Optional[int] = None, suggestion_status: Optional[str] = None) -> Food:
        food = Food(name=name, calories=calories, protein=protein, owner_id=owner_id, suggestion_status=suggestion_status)
        db.add(food)
        db.commit()
        db.refresh(food)
        return food

    def update(self, db: Session, food: Food, name: str, calories: float, protein: float,
               suggestion_status: Optional[str]) -> Food:
        food.name, food.calories, food.protein, food.suggestion_status = name, calories, protein, suggestion_status
        db.commit()
        db.refresh(food)
        return food

    def approve(self, db: Session, food: Food) -> Food:
        """Moves a suggestion into the global pool in place; the id is unchanged."""
        food.owner_id, food.suggestion_status = None, None
        db.commit()
        db.refresh(food)
        return food

    def reject(self, db: Session, food: Food) -> Food:
        food.suggestion_status = SUGGESTION_REJECTED
        db.commit()
        db.refresh(food)
        return food


class UserRecordRepository:
    def create(self, db: Session, user_id: int, food_id: int, amount_grams: float, record_date: date) -> UserRecord:
        record = UserRecord(user_id=user_id, food_id=food_id, amount_grams=amount_grams, record_date=record_date)
        db.add(record)
        db.commit()
        db.refresh(record)
        return record

    def list_for_user_and_date(self, db: Session, user_id: int, record_date: date) -> list[UserRecord]:
        return (
            db.query(UserRecord)
            .options(joinedload(UserRecord.food))
            .filter(UserRecord.user_id == user_id, UserRecord.record_date == record_date)
            .order_by(UserRecord.id)
            .all()
        )

    def delete_owned(self, db: Session, record_id: int, user_id: int) -> bool:
        record = db.query(UserRecord).filter(UserRecord.id == record_id, UserRecord.user_id == user_id).first()
        if not record:
            return False
        db.delete(record)
        db.commit()
        return True
