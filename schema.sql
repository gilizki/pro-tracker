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

-- Friends feature. A pending request or an accepted friendship between two users.
-- pair_key = '<smaller user id>:<larger user id>' allows only one row per pair.
CREATE TABLE friendships (
    id INTEGER PRIMARY KEY,
    requester_id INTEGER NOT NULL REFERENCES users(id),
    addressee_id INTEGER NOT NULL REFERENCES users(id),
    status VARCHAR(16) NOT NULL,
    pair_key VARCHAR(64) NOT NULL UNIQUE,
    created_at DATETIME NOT NULL,
    CONSTRAINT ck_friendships_not_self CHECK (requester_id <> addressee_id),
    CONSTRAINT ck_friendships_status CHECK (status IN ('pending', 'accepted'))
);

CREATE INDEX ix_friendships_requester_id ON friendships(requester_id);
CREATE INDEX ix_friendships_addressee_id ON friendships(addressee_id);

-- One permanent invite code per user, replaced only when the user regenerates it.
CREATE TABLE friend_invite_codes (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL UNIQUE REFERENCES users(id),
    code VARCHAR(16) NOT NULL UNIQUE
);
