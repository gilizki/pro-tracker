"""Application services coordinate repositories and enforce business rules."""
from datetime import date
from typing import Optional

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.tracker.models import SUGGESTION_PENDING, SUGGESTION_REJECTED, Food, User
from app.tracker.repositories import FoodRepository, SessionRepository, UserRecordRepository, UserRepository, normalize_food_name
from app.core.security import PasswordHasher, TokenGenerator


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


def require_admin(user: User) -> None:
    if not user.is_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin access required")


class FoodService:
    """Global vs personal food rules. Authorization lives here (not only in the UI):

    - users see global foods plus their own personal foods, nothing else;
    - another user's personal food is reported as "not found", even to admins;
    - only admins create, edit or delete global foods and approve/reject suggestions.
    """

    def __init__(self):
        self.foods = FoodRepository()

    # ---------------------------------------------------------------- user actions
    def list_foods(self, db: Session, user: User) -> list[dict]:
        return [self.to_dict(food, user) for food in self.foods.list_visible(db, user.id)]

    def create_food(self, db: Session, user: User, name: str, calories: float, protein: float,
                    suggest: bool = False, is_global: bool = False, allow_duplicate: bool = False) -> dict:
        name = self._clean_name(name)
        if is_global:
            require_admin(user)
            if suggest:
                raise HTTPException(status_code=422, detail="A global food cannot also be a suggestion")
            self._block_global_duplicate(db, name)
            food = self.foods.create(db, name, calories, protein)
        else:
            if suggest:
                self._block_global_duplicate(db, name, code="duplicate_global_suggestion")
            elif not allow_duplicate:
                self._warn_global_duplicate(db, name)
            food = self.foods.create(db, name, calories, protein, owner_id=user.id,
                                     suggestion_status=SUGGESTION_PENDING if suggest else None)
        return self.to_dict(food, user)

    def update_food(self, db: Session, user: User, food_id: int, name: str, calories: float, protein: float,
                    suggest: Optional[bool] = None, allow_duplicate: bool = False) -> dict:
        food = self._find_visible_or_404(db, user, food_id)
        name = self._clean_name(name)
        if food.owner_id is None:
            require_admin(user)
            if suggest:
                raise HTTPException(status_code=422, detail="A global food cannot be suggested")
            self._block_global_duplicate(db, name, exclude_id=food.id)
            return self.to_dict(self.foods.update(db, food, name, calories, protein, None), user)

        # From here on the food is the caller's own personal food (find_visible guarantees it).
        changed = (name, calories, protein) != (food.name, food.calories, food.protein)
        new_status = food.suggestion_status
        if suggest is True:
            if food.suggestion_status == SUGGESTION_REJECTED and not changed:
                raise HTTPException(status_code=409, detail={
                    "code": "edit_before_resuggest",
                    "message": "This suggestion was rejected. Edit the food before suggesting it again."})
            new_status = SUGGESTION_PENDING
        elif suggest is False and food.suggestion_status == SUGGESTION_PENDING:
            new_status = None  # withdraw the suggestion

        if new_status == SUGGESTION_PENDING:
            self._block_global_duplicate(db, name, code="duplicate_global_suggestion")
        elif normalize_food_name(name) != normalize_food_name(food.name) and not allow_duplicate:
            self._warn_global_duplicate(db, name)
        return self.to_dict(self.foods.update(db, food, name, calories, protein, new_status), user)

    def delete_food(self, db: Session, user: User, food_id: int, keep_personal_copies: bool = False) -> dict:
        food = self._find_visible_or_404(db, user, food_id)  # another user's personal food -> 404, even for admins
        if food.owner_id is None:
            require_admin(user)
            users, records = self.foods.usage(db, food.id)
            if not records:
                self.foods.delete(db, food)
                return {"status": "deleted", "personal_copies": 0, "records_moved": 0}
            # Used by someone: only with explicit confirmation, after which each of those users keeps a personal copy.
            if not keep_personal_copies:
                raise HTTPException(status_code=409, detail={
                    "code": "food_in_use_global", "users": users, "records": records,
                    "message": f"{users} user(s) logged this food ({records} record(s)). Confirm with "
                               "keep_personal_copies=true to remove it from the global pool and keep a personal copy for them."})
            copies, moved = self.foods.delete_global_keeping_personal_copies(db, food)
            return {"status": "deleted", "personal_copies": copies, "records_moved": moved}
        # Own personal food. Records reference foods by id; deleting a used food would break meal history.
        if self.foods.has_records(db, food.id):
            raise HTTPException(status_code=409, detail={
                "code": "food_in_use", "message": "This food is used in meal records and cannot be deleted."})
        self.foods.delete(db, food)
        return {"status": "deleted"}

    def global_food_usage(self, db: Session, admin: User, food_id: int) -> dict:
        """Preview for the admin's delete confirmation: who would get a personal copy."""
        require_admin(admin)
        food = self.foods.find_visible(db, food_id, admin.id)
        if not food or food.owner_id is not None:
            raise HTTPException(status_code=404, detail="Global food not found")
        users, records = self.foods.usage(db, food.id)
        return {"food_id": food.id, "users": users, "records": records}

    # ---------------------------------------------------------------- admin actions
    def list_suggestions(self, db: Session, admin: User) -> list[dict]:
        require_admin(admin)
        result = []
        for food in self.foods.list_pending(db):
            duplicate = self.foods.find_global_by_name(db, food.name)
            result.append({**self.to_dict(food, admin), "owner_username": food.owner.username,
                           "duplicate_of_global_id": duplicate.id if duplicate else None})
        return result

    def approve_suggestion(self, db: Session, admin: User, food_id: int) -> dict:
        require_admin(admin)
        food = self._find_pending_or_404(db, food_id)
        self._block_global_duplicate(db, food.name)
        return self.to_dict(self.foods.approve(db, food), admin)

    def reject_suggestion(self, db: Session, admin: User, food_id: int) -> None:
        require_admin(admin)
        self.foods.reject(db, self._find_pending_or_404(db, food_id))

    # ---------------------------------------------------------------- helpers
    @staticmethod
    def to_dict(food: Food, user: User) -> dict:
        is_global = food.owner_id is None
        return {"id": food.id, "name": food.name, "calories": food.calories, "protein": food.protein,
                "scope": "global" if is_global else "personal", "suggestion_status": food.suggestion_status,
                "can_edit": bool(user.is_admin) if is_global else food.owner_id == user.id}

    @staticmethod
    def _clean_name(name: str) -> str:
        name = name.strip()
        if not name:
            raise HTTPException(status_code=422, detail="Food name is required")
        return name

    def _find_visible_or_404(self, db: Session, user: User, food_id: int) -> Food:
        food = self.foods.find_visible(db, food_id, user.id)
        if not food:
            raise HTTPException(status_code=404, detail="Food not found")
        return food

    def _find_pending_or_404(self, db: Session, food_id: int) -> Food:
        food = self.foods.find_pending(db, food_id)
        if not food:
            raise HTTPException(status_code=404, detail="Pending suggestion not found")
        return food

    def _block_global_duplicate(self, db: Session, name: str, exclude_id: Optional[int] = None,
                                code: str = "duplicate_global_blocked") -> None:
        duplicate = self.foods.find_global_by_name(db, name, exclude_id=exclude_id)
        if duplicate:
            raise HTTPException(status_code=409, detail={
                "code": code, "food_id": duplicate.id,
                "message": f'A global food named "{duplicate.name}" already exists.'})

    def _warn_global_duplicate(self, db: Session, name: str) -> None:
        """Personal foods may share a global name, but only after the user confirms."""
        duplicate = self.foods.find_global_by_name(db, name)
        if duplicate:
            raise HTTPException(status_code=409, detail={
                "code": "duplicate_global", "food_id": duplicate.id,
                "message": f'A global food named "{duplicate.name}" already exists. Save a personal copy anyway?'})


class RecordService:
    def __init__(self):
        self.foods = FoodRepository()
        self.records = UserRecordRepository()

    def add_record(self, db: Session, user: User, food_id: int, amount_grams: float, record_date: date):
        if not self.foods.find_visible(db, food_id, user.id):  # never another user's personal food
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
