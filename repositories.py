"""Repository/DAO layer. All SQLAlchemy queries are contained here."""
from __future__ import annotations

from datetime import date
from typing import Optional

from sqlalchemy.orm import Session, joinedload

from models import AuthSession, Food, User, UserRecord


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
    def list_all(self, db: Session) -> list[Food]:
        return db.query(Food).order_by(Food.name).all()

    def find_by_id(self, db: Session, food_id: int) -> Optional[Food]:
        return db.get(Food, food_id)

    def create(self, db: Session, name: str, calories: float, protein: float) -> Food:
        food = Food(name=name, calories=calories, protein=protein)
        db.add(food)
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
