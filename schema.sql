-- Database creation queries (SQLite/PostgreSQL compatible with minor SERIAL syntax changes).
CREATE TABLE users (
    id INTEGER PRIMARY KEY,
    username VARCHAR(64) NOT NULL UNIQUE,
    password_hash VARCHAR(256) NOT NULL
);

-- This is global, deliberately not owned by a user.
CREATE TABLE foods (
    id INTEGER PRIMARY KEY,
    name VARCHAR(128) NOT NULL,
    calories FLOAT NOT NULL,
    protein FLOAT NOT NULL
);

-- This table isolates every user's personal history.
CREATE TABLE user_records (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    food_id INTEGER NOT NULL REFERENCES foods(id),
    amount_grams FLOAT NOT NULL,
    record_date DATE NOT NULL
);

CREATE TABLE auth_sessions (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    token_hash VARCHAR(64) NOT NULL UNIQUE
);

CREATE INDEX ix_user_records_user_date ON user_records(user_id, record_date);
