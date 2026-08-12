"""HTTP DTOs. Keeping them separate prevents transport concerns leaking into models."""
from datetime import date

from pydantic import BaseModel, Field


class Credentials(BaseModel):
    username: str = Field(min_length=3, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")
    password: str = Field(min_length=8, max_length=256)


class FoodCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    calories: float = Field(ge=0)
    protein: float = Field(ge=0)


class RecordCreate(BaseModel):
    food_id: int
    amount_grams: float = Field(gt=0)
    date: date
