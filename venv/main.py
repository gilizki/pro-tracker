from fastapi import FastAPI, Depends
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session
from pydantic import BaseModel
from fastapi.responses import HTMLResponse
import os
import database

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

def get_db():
    db = database.SessionLocal()
    try:
        yield db
    finally:
        db.close()

class FoodCreate(BaseModel):
    name: str
    calories: float
    protein: float

class LogCreate(BaseModel):
    food_id: int
    amount_grams: float
    date: str

@app.get("/", response_class=HTMLResponse)
def read_root():
    if os.path.exists("frontend/index.html"):
        with open("frontend/index.html", "r", encoding="utf-8") as f:
            return f.read()
    return "Pro Tracker Server is running!"

@app.get("/foods/")
def get_all_foods(db: Session = Depends(get_db)):
    foods = db.query(database.Food).all()
    return foods

@app.post("/foods/")
def create_food(food: FoodCreate, db: Session = Depends(get_db)):
    new_food = database.Food(name=food.name, calories=food.calories, protein=food.protein)
    db.add(new_food)
    db.commit()
    db.refresh(new_food)
    return new_food

@app.post("/logs/")
def create_log(log_entry: LogCreate, db: Session = Depends(get_db)):
    new_log = database.Log(
        food_id=log_entry.food_id,
        amount_grams=log_entry.amount_grams,
        date=log_entry.date
    )
    db.add(new_log)
    db.commit()
    db.refresh(new_log)
    return new_log

@app.get("/summary/{date}")
def get_daily_summary(date: str, db: Session = Depends(get_db)):
    logs = db.query(database.Log).filter(database.Log.date == date).all()
    
    total_calories = 0.0
    total_protein = 0.0
    details = []

    for log in logs:
        food = db.query(database.Food).filter(database.Food.id == log.food_id).first()
        if food:
            factor = log.amount_grams / 100.0
            calories_eaten = food.calories * factor
            protein_eaten = food.protein * factor
            
            total_calories += calories_eaten
            total_protein += protein_eaten
            
            details.append({
                "log_id": log.id,  # נוסף: המזהה של הארוחה לצורך מחיקה
                "food_name": food.name,
                "amount_grams": log.amount_grams,
                "calories": round(calories_eaten, 1),
                "protein": round(protein_eaten, 1)
            })

    return {
        "date": date,
        "total_calories": round(total_calories, 1),
        "total_protein": round(total_protein, 1),
        "meals": details
    }

# בקשת ה-DELETE החדשה למחיקת ארוחה מהיומן
@app.delete("/logs/{log_id}")
def delete_log(log_id: int, db: Session = Depends(get_db)):
    log = db.query(database.Log).filter(database.Log.id == log_id).first()
    if log:
        db.delete(log)
        db.commit()
        return {"status": "deleted"}
    return {"error": "not found"}

# בקשת DELETE למחיקת מאכל ממאגר המאכלים הכללי
@app.delete("/foods/{food_id}")
def delete_food(food_id: int, db: Session = Depends(get_db)):
    food = db.query(database.Food).filter(database.Food.id == food_id).first()
    if food:
        db.delete(food)
        db.commit()
        return {"status": "deleted"}
    return {"error": "not found"}