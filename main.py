"""HTTP controller layer. It delegates all persistence to services/repositories."""
from __future__ import annotations

import os
from datetime import date
from typing import Optional

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session

from database import Base, engine, get_db
from models import User  # Ensures every mapped model is registered before create_all.
from schemas import Credentials, FoodCreate, RecordCreate
from services import AuthenticationService, FoodService, RecordService

Base.metadata.create_all(bind=engine)

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


@app.get("/", response_class=HTMLResponse)
def read_root():
    with open(os.path.join("frontend", "index.html"), encoding="utf-8") as page:
        return page.read()


@app.post("/auth/signup")
def sign_up(credentials: Credentials, db: Session = Depends(get_db)):
    user, token = auth_service.sign_up(db, credentials.username, credentials.password)
    return {"token": token, "user": {"id": user.id, "username": user.username}}


@app.post("/auth/login")
def log_in(credentials: Credentials, db: Session = Depends(get_db)):
    user, token = auth_service.log_in(db, credentials.username, credentials.password)
    return {"token": token, "user": {"id": user.id, "username": user.username}}


@app.post("/auth/logout")
def log_out(token: str = Depends(bearer_token), db: Session = Depends(get_db)):
    auth_service.log_out(db, token)
    return {"status": "logged_out"}


@app.get("/auth/me")
def me(user: User = Depends(current_user)):
    return {"id": user.id, "username": user.username}


@app.get("/foods/")
def get_all_foods(_: User = Depends(current_user), db: Session = Depends(get_db)):
    return food_service.list_foods(db)


@app.post("/foods/")
def create_food(food: FoodCreate, _: User = Depends(current_user), db: Session = Depends(get_db)):
    return food_service.create_food(db, food.name, food.calories, food.protein)


@app.post("/records/")
def create_record(record: RecordCreate, user: User = Depends(current_user), db: Session = Depends(get_db)):
    return record_service.add_record(db, user, record.food_id, record.amount_grams, record.date)


@app.get("/summary/{record_date}")
def get_daily_summary(record_date: date, user: User = Depends(current_user), db: Session = Depends(get_db)):
    return record_service.summary(db, user, record_date)


@app.delete("/records/{record_id}")
def delete_record(record_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    record_service.delete_record(db, user, record_id)
    return {"status": "deleted"}
