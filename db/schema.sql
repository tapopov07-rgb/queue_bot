-- Схема БД. Время везде хранится как unix time (UTC, секунды).

CREATE TABLE IF NOT EXISTS users (
    id          INTEGER PRIMARY KEY,              -- telegram user id
    first_name  TEXT    NOT NULL,
    last_name   TEXT,
    username    TEXT,
    can_dm      INTEGER NOT NULL DEFAULT 0,       -- нажал /start в личке
    created_at  INTEGER NOT NULL,
    updated_at  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS chats (
    id          INTEGER PRIMARY KEY,              -- telegram chat id
    title       TEXT    NOT NULL,
    is_active   INTEGER NOT NULL DEFAULT 1,       -- 0 — бота удалили из группы
    added_by    INTEGER REFERENCES users(id),
    created_at  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS streams (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    title           TEXT    NOT NULL,
    semester_start  TEXT,                         -- YYYY-MM-DD, этап 3
    created_at      INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS stream_chats (
    stream_id  INTEGER NOT NULL REFERENCES streams(id) ON DELETE CASCADE,
    chat_id    INTEGER NOT NULL REFERENCES chats(id) ON DELETE CASCADE ON UPDATE CASCADE,
    PRIMARY KEY (stream_id, chat_id)
);

CREATE TABLE IF NOT EXISTS stream_admins (
    stream_id  INTEGER NOT NULL REFERENCES streams(id) ON DELETE CASCADE,
    user_id    INTEGER NOT NULL REFERENCES users(id),
    PRIMARY KEY (stream_id, user_id)
);

-- участники потока, известные боту (нажимали кнопки / писали команды в группах потока).
-- Telegram не даёт боту список участников группы, поэтому собираем сами.
CREATE TABLE IF NOT EXISTS stream_members (
    stream_id  INTEGER NOT NULL REFERENCES streams(id) ON DELETE CASCADE,
    user_id    INTEGER NOT NULL REFERENCES users(id),
    last_seen  INTEGER NOT NULL,
    PRIMARY KEY (stream_id, user_id)
);

CREATE TABLE IF NOT EXISTS schedule (                -- этап 3
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    stream_id          INTEGER NOT NULL REFERENCES streams(id) ON DELETE CASCADE,
    subject            TEXT    NOT NULL,
    weekday            INTEGER NOT NULL CHECK (weekday BETWEEN 0 AND 6),
    start_time         TEXT    NOT NULL,             -- HH:MM
    duration_min       INTEGER NOT NULL DEFAULT 90,
    parity             TEXT    NOT NULL DEFAULT 'all' CHECK (parity IN ('all', 'even', 'odd')),
    open_before_hours  INTEGER NOT NULL DEFAULT 24,
    is_active          INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS queues (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    stream_id    INTEGER NOT NULL REFERENCES streams(id) ON DELETE CASCADE,
    schedule_id  INTEGER REFERENCES schedule(id) ON DELETE SET NULL,
    subject      TEXT    NOT NULL,
    starts_at    INTEGER NOT NULL,
    ends_at      INTEGER NOT NULL,
    opens_at     INTEGER NOT NULL,
    status       TEXT    NOT NULL CHECK (status IN ('scheduled', 'open', 'closed')),
    created_by   INTEGER REFERENCES users(id),      -- NULL — создана планировщиком
    created_at   INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_queues_status ON queues(status);

CREATE TABLE IF NOT EXISTS queue_entries (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    queue_id    INTEGER NOT NULL REFERENCES queues(id) ON DELETE CASCADE,
    position    INTEGER NOT NULL,                  -- 1..N без пропусков
    user_id     INTEGER NOT NULL REFERENCES users(id),  -- владелец записи (получает уведомления, меняется)
    guest_name  TEXT,                              -- человек не из списка, вписан текстом; владелец — тот, кто вписал
    added_by    INTEGER NOT NULL REFERENCES users(id),  -- кто создал запись (сам или записавший одногруппник)
    created_at  INTEGER NOT NULL
);
-- одна своя запись и одна вписанная текстом на пользователя в очереди
-- (общий лимит «записать можно одного человека» проверяет queue_service)
CREATE UNIQUE INDEX IF NOT EXISTS ux_entry_self  ON queue_entries(queue_id, user_id) WHERE guest_name IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS ux_entry_guest ON queue_entries(queue_id, user_id) WHERE guest_name IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_entries_user ON queue_entries(user_id);

CREATE TABLE IF NOT EXISTS queue_messages (
    queue_id    INTEGER NOT NULL REFERENCES queues(id) ON DELETE CASCADE,
    chat_id     INTEGER NOT NULL REFERENCES chats(id) ON DELETE CASCADE ON UPDATE CASCADE,
    message_id  INTEGER NOT NULL,
    PRIMARY KEY (queue_id, chat_id)
);

CREATE TABLE IF NOT EXISTS queue_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    queue_id    INTEGER NOT NULL REFERENCES queues(id) ON DELETE CASCADE,
    actor_id    INTEGER,                           -- NULL — система
    type        TEXT    NOT NULL,
    payload     TEXT,                              -- JSON
    created_at  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS swap_requests (           -- этап 2
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    queue_id       INTEGER NOT NULL REFERENCES queues(id) ON DELETE CASCADE,
    from_entry_id  INTEGER NOT NULL REFERENCES queue_entries(id) ON DELETE CASCADE,
    to_entry_id    INTEGER NOT NULL REFERENCES queue_entries(id) ON DELETE CASCADE,
    status         TEXT    NOT NULL DEFAULT 'pending'
                   CHECK (status IN ('pending', 'accepted', 'declined', 'cancelled', 'expired')),
    created_at     INTEGER NOT NULL
);
