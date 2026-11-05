-- Database creation queries (SQLite/PostgreSQL compatible with minor SERIAL syntax changes).
CREATE TABLE users (
    id INTEGER PRIMARY KEY,
    username VARCHAR(64) NOT NULL UNIQUE,
    password_hash VARCHAR(256) NOT NULL,
    is_admin BOOLEAN NOT NULL DEFAULT FALSE
);

-- owner_id NULL = global food (visible to everyone, managed by admins).
-- owner_id set   = personal food, visible only to that user.
-- suggestion_status: NULL, 'pending' (suggested for the global pool) or 'rejected'.
-- Approving a suggestion clears owner_id in place, so the food id never changes.
CREATE TABLE foods (
    id INTEGER PRIMARY KEY,
    name VARCHAR(128) NOT NULL,
    calories FLOAT NOT NULL,
    protein FLOAT NOT NULL,
    owner_id INTEGER REFERENCES users(id),
    suggestion_status VARCHAR(16),
    CONSTRAINT ck_foods_suggestion_status CHECK (suggestion_status IS NULL OR suggestion_status IN ('pending', 'rejected'))
);

CREATE INDEX ix_foods_owner_id ON foods(owner_id);

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

-- Push notifications. One row per subscribed device/browser; a user can have several.
CREATE TABLE push_subscriptions (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    endpoint VARCHAR(1024) NOT NULL UNIQUE,
    p256dh VARCHAR(256) NOT NULL,
    auth VARCHAR(64) NOT NULL,
    created_at DATETIME NOT NULL
);

CREATE INDEX ix_push_subscriptions_user_id ON push_subscriptions(user_id);

-- Notifications sent about a friend (never shown in the app). day = tracker day.
-- Once per type per friend per day, and at most DAILY_CAP (slots 1..2) per recipient per day.
CREATE TABLE sent_notifications (
    id INTEGER PRIMARY KEY,
    recipient_id INTEGER NOT NULL REFERENCES users(id),
    friend_id INTEGER NOT NULL REFERENCES users(id),
    type VARCHAR(32) NOT NULL,
    day DATE NOT NULL,
    slot INTEGER NOT NULL,
    created_at DATETIME NOT NULL,
    CONSTRAINT uq_sent_once_per_day UNIQUE (recipient_id, friend_id, type, day),
    CONSTRAINT uq_sent_daily_slot UNIQUE (recipient_id, day, slot),
    CONSTRAINT ck_sent_not_self CHECK (recipient_id <> friend_id),
    CONSTRAINT ck_sent_slot_positive CHECK (slot >= 1),
    CONSTRAINT ck_sent_type CHECK (type IN ('protein_machine', 'midnight_munch', 'bulldozer'))
);
