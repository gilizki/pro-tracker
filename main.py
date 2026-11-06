"""HTTP controller layer. It delegates all persistence to services/repositories."""
from __future__ import annotations

import os
from datetime import date, datetime
from typing import Optional

from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session

from app.core.database import (Base, engine, get_db, migrate_legacy_user_passwords, require_food_pools_schema,
                               require_sent_notifications_schema)
from app.tracker.models import User  # Ensures every mapped model is registered before create_all.
from app.tracker.schemas import Credentials, FoodCreate, FoodUpdate, RecordCreate
from app.tracker.services import AuthenticationService, FoodService, RecordService, require_admin
from app.friends.routes import build_friends_router  # Also registers the friends tables before create_all.
from app.notifications.routes import build_notifications_router, israel_now, schedule_meal_notifications  # Also registers its tables.

Base.metadata.create_all(bind=engine)
migrate_legacy_user_passwords()
require_food_pools_schema()  # checks only; run tools/migrate_food_pools.py to upgrade
require_sent_notifications_schema()  # checks only; run tools/migrate_sent_notifications.py to upgrade

app = FastAPI(title="Pro Tracker")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"], allow_credentials=False)
app.mount("/frontend", StaticFiles(directory="frontend"), name="frontend")

auth_service, food_service, record_service = AuthenticationService(), FoodService(), RecordService()


def bearer_token(authorization: Optional[str] = Header(default=None)) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Authentication required")
    return authorization.removeprefix("Bearer ")


def current_user(token: str = Depends(bearer_token), db: Session = Depends(get_db)) -> User:
    return auth_service.current_user(db, token)


def admin_user(user: User = Depends(current_user)) -> User:
    require_admin(user)  # the service methods check again (defense in depth)
    return user


def user_dict(user: User) -> dict:
    return {"id": user.id, "username": user.username, "is_admin": bool(user.is_admin)}


@app.get("/", response_class=HTMLResponse)
def read_root():
    with open(os.path.join("frontend", "index.html"), encoding="utf-8") as page:
        return page.read()


@app.post("/auth/signup")
def sign_up(credentials: Credentials, db: Session = Depends(get_db)):
    user, token = auth_service.sign_up(db, credentials.username, credentials.password)
    return {"token": token, "user": user_dict(user)}


@app.post("/auth/login")
def log_in(credentials: Credentials, db: Session = Depends(get_db)):
    user, token = auth_service.log_in(db, credentials.username, credentials.password)
    return {"token": token, "user": user_dict(user)}


@app.post("/auth/logout")
def log_out(token: str = Depends(bearer_token), db: Session = Depends(get_db)):
    auth_service.log_out(db, token)
    return {"status": "logged_out"}


@app.get("/auth/me")
def me(user: User = Depends(current_user)):
    return user_dict(user)


@app.get("/foods/")
def get_all_foods(user: User = Depends(current_user), db: Session = Depends(get_db)):
    """Global foods plus the caller's own personal foods."""
    return food_service.list_foods(db, user)


@app.post("/foods/")
def create_food(food: FoodCreate, user: User = Depends(current_user), db: Session = Depends(get_db)):
    return food_service.create_food(db, user, food.name, food.calories, food.protein,
                                    suggest=food.suggest, is_global=food.is_global, allow_duplicate=food.allow_duplicate)


@app.put("/foods/{food_id}")
def update_food(food_id: int, food: FoodUpdate, user: User = Depends(current_user), db: Session = Depends(get_db)):
    return food_service.update_food(db, user, food_id, food.name, food.calories, food.protein,
                                    suggest=food.suggest, allow_duplicate=food.allow_duplicate)


@app.get("/foods/{food_id}/usage")
def food_usage(food_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    """Delete preview: log entries (and users) that use this food. Owner for personal foods, admins for global ones."""
    return food_service.food_usage(db, user, food_id)


@app.delete("/foods/{food_id}")
def delete_food(food_id: int, confirm_records: int = 0, user: User = Depends(current_user),
                db: Session = Depends(get_db)):
    """Deletes the food and every log entry using it. confirm_records = the count the user confirmed (see FoodService)."""
    return food_service.delete_food(db, user, food_id, confirm_records=confirm_records)


@app.get("/admin/suggestions")
def list_suggestions(admin: User = Depends(admin_user), db: Session = Depends(get_db)):
    return food_service.list_suggestions(db, admin)


@app.post("/admin/suggestions/{food_id}/approve")
def approve_suggestion(food_id: int, admin: User = Depends(admin_user), db: Session = Depends(get_db)):
    return food_service.approve_suggestion(db, admin, food_id)


@app.post("/admin/suggestions/{food_id}/reject")
def reject_suggestion(food_id: int, admin: User = Depends(admin_user), db: Session = Depends(get_db)):
    food_service.reject_suggestion(db, admin, food_id)
    return {"status": "rejected"}


@app.post("/records/")
def create_record(record: RecordCreate, background_tasks: BackgroundTasks,
                  logged_at: datetime = Depends(israel_now),  # first dependency: resolved before auth
                  user: User = Depends(current_user), db: Session = Depends(get_db)):
    saved = record_service.add_record(db, user, record.food_id, record.amount_grams, record.date)
    schedule_meal_notifications(background_tasks, user.id, record.date, logged_at, saved.id)
    return saved


@app.get("/summary/{record_date}")
def get_daily_summary(record_date: date, user: User = Depends(current_user), db: Session = Depends(get_db)):
    return record_service.summary(db, user, record_date)


@app.delete("/records/{record_id}")
def delete_record(record_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    record_service.delete_record(db, user, record_id)
    return {"status": "deleted"}


app.include_router(build_friends_router(current_user))
app.include_router(build_notifications_router(current_user))
