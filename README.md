<div align="center">

# 🍎 Pro Tracker

> **A full-stack nutrition tracking application for managing meals, calories, and protein.**

Pro Tracker lets users create an account, log the food they eat, and track their daily calorie and protein intake through a clean **Hebrew RTL interface**.

</div>

---

## ✨ Features

- 👤 User registration & login
- 🔐 Secure token-based authentication
- 🍎 Shared food catalogue
- 🍽️ Meal tracking by date and grams
- 🔥 Automatic calorie calculation
- 💪 Automatic protein calculation
- 📅 Daily nutrition summaries
- 📱 Mobile-friendly RTL interface

---

## 🛠️ Tech Stack

| Backend | Frontend | Security |
| :--- | :--- | :--- |
| Python | HTML | PBKDF2-HMAC-SHA-256 password hashing |
| FastAPI | CSS | Secure session tokens |
| SQLAlchemy | Vanilla JavaScript | Hashed tokens stored in the database |
| SQLite / PostgreSQL |  |  |

---

## 🏗️ Architecture

```text
Frontend
    ↓
FastAPI API
    ↓
Service Layer
    ↓
Repository Layer
    ↓
SQLAlchemy
    ↓
SQLite / PostgreSQL
```

The project uses a layered architecture to keep API controllers, business logic, and database operations separated.

---

## 📂 Structure

```text
├── main.py                 # FastAPI app & core routes (entry point)
├── app/
│   ├── core/               # Shared infrastructure
│   │   ├── database.py     #   DB configuration
│   │   ├── security.py     #   Password hashing & tokens
│   │   └── israel_time.py  #   Tracker-day rule (02:00–01:59 Israel time)
│   ├── tracker/            # Users, foods & meal records
│   │   ├── models.py       #   Database models
│   │   ├── schemas.py      #   Validation & DTOs
│   │   ├── services.py     #   Business logic
│   │   └── repositories.py #   Database access
│   ├── friends/            # Friends feature (models, schemas, repository, service, routes)
│   └── notifications/      # Push notifications (config, models, push, repository, service, routes)
├── frontend/               # Web application (index.html, JS, PWA files)
├── tests/                  # pytest suite
└── tools/                  # One-off scripts (VAPID keys, migrations)
```

---

## 🚀 Run Locally

```bash
pip install -r requirements.txt
uvicorn main:app --reload
```

Then open the address provided by Uvicorn.

FastAPI also provides interactive API documentation at:

```text
/docs
```

To run the tests: `pip install -r requirements-dev.txt`, then `pytest` (the day-boundary tests also need Node.js).

---

## 🧮 Nutrition Calculation

Nutrition values are stored per 100g:

```text
factor = amount_grams / 100
calories = food_calories × factor
protein = food_protein × factor
```

Daily totals are automatically calculated from the user's logged meals.

---

## 👥 Friends

Users can add a friend by username or by invite code, and the other user has to accept the request.
Friends see only each other's daily totals (calories and protein), never meals or foods.
You can regenerate your invite code or remove a friend at any time.

A day runs from 02:00 to 01:59 Israel time, so a meal logged at 01:30 counts toward the previous day.

---

## 🔔 Notifications

Friends get a push notification on their phone when you:

- 💪 **מכונת חלבון**: pass 120 g protein in a day
- 🌙 **מאנץ' לילי**: log a meal between 00:00 and 01:59 (silent)
- 🚜 **בולדוזר**: pass 2000 kcal before 20:00

Each type is sent at most once per day per friend, one per meal, and at most 2 per day in total.
Thresholds and texts live in `app/notifications/config.py`. Tap "הפעל התראות" in the app to enable
(on iPhone, add the site to the Home Screen and open it from there first).

Push needs three environment variables (never commit them): `VAPID_PUBLIC_KEY`, `VAPID_PRIVATE_KEY`
and `VAPID_SUBJECT`. Generate the keys once with `python tools/generate_vapid_keys.py`.
Without them the app works as before, without notifications.

---

<div align="center">

### 🍎 Pro Tracker

**Track your food. Know your numbers.**

</div>
