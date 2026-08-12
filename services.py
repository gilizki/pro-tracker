"""Application services coordinate repositories and enforce business rules."""
from datetime import date

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from models import Food, User
from repositories import FoodRepository, SessionRepository, UserRecordRepository, UserRepository
from security import PasswordHasher, TokenGenerator


class AuthenticationService:
    def __init__(self):
        self.users = UserRepository()
        self.sessions = SessionRepository()

    def sign_up(self, db: Session, username: str, password: str) -> tuple[User, str]:
        if self.users.find_by_username(db, username):
            raise HTTPException(status_code=409, detail="Username is already in use")
        user = self.users.create(db, username, PasswordHasher.hash(password))
        return user, self._new_session(db, user.id)

    def log_in(self, db: Session, username: str, password: str) -> tuple[User, str]:
        user = self.users.find_by_username(db, username)
        if not user or not PasswordHasher.verify(password, user.password_hash):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid username or password")
        return user, self._new_session(db, user.id)

    def current_user(self, db: Session, token: str) -> User:
        user = self.sessions.user_for_token(db, TokenGenerator.digest(token))
        if not user:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired session")
        return user

    def log_out(self, db: Session, token: str) -> None:
        self.sessions.delete(db, TokenGenerator.digest(token))

    def _new_session(self, db: Session, user_id: int) -> str:
        token = TokenGenerator.create()
        self.sessions.create(db, user_id, TokenGenerator.digest(token))
        return token


class FoodService:
    def __init__(self):
        self.foods = FoodRepository()

    def list_foods(self, db: Session) -> list[Food]:
        return self.foods.list_all(db)

    def create_food(self, db: Session, name: str, calories: float, protein: float) -> Food:
        return self.foods.create(db, name.strip(), calories, protein)


class RecordService:
    def __init__(self):
        self.foods = FoodRepository()
        self.records = UserRecordRepository()

    def add_record(self, db: Session, user: User, food_id: int, amount_grams: float, record_date: date):
        if not self.foods.find_by_id(db, food_id):
            raise HTTPException(status_code=404, detail="Food not found")
        return self.records.create(db, user.id, food_id, amount_grams, record_date)

    def summary(self, db: Session, user: User, record_date: date) -> dict:
        records = self.records.list_for_user_and_date(db, user.id, record_date)
        meals, calories, protein = [], 0.0, 0.0
        for record in records:
            factor = record.amount_grams / 100
            meal_calories, meal_protein = record.food.calories * factor, record.food.protein * factor
            calories += meal_calories
            protein += meal_protein
            meals.append({"record_id": record.id, "food_name": record.food.name, "amount_grams": record.amount_grams,
                          "calories": round(meal_calories, 1), "protein": round(meal_protein, 1)})
        return {"date": record_date, "total_calories": round(calories, 1), "total_protein": round(protein, 1), "meals": meals}

    def delete_record(self, db: Session, user: User, record_id: int) -> None:
        if not self.records.delete_owned(db, record_id, user.id):
            raise HTTPException(status_code=404, detail="Record not found")
