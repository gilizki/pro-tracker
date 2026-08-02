from sqlalchemy import create_engine, Column, Integer, String, Float, ForeignKey
from sqlalchemy.orm import declarative_base, sessionmaker

# הגדרת קובץ מסד הנתונים (ייווצר אוטומטית באותה תיקייה)
SQLALCHEMY_DATABASE_URL = "sqlite:///./tracker.db"

engine = create_engine(SQLALCHEMY_DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

# מחלקה המייצגת את טבלת המאכלים
class Food(Base):
    __tablename__ = "foods"
    
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, index=True)
    calories = Column(Float)  # קלוריות ל-100 גרם או ליחידה
    protein = Column(Float)   # חלבון בגרמים

# מחלקה המייצגת את טבלת יומן האכילה
class Log(Base):
    __tablename__ = "logs"
    
    id = Column(Integer, primary_key=True, index=True)
    food_id = Column(Integer, ForeignKey("foods.id"))
    amount_grams = Column(Float)
    date = Column(String)  # תאריך בפורמט YYYY-MM-DD

# הפקודה שיוצרת את הטבלאות בפועל
Base.metadata.create_all(bind=engine)