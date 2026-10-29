"""Запросы к БД. Все функции принимают открытое соединение первым аргументом."""
import json
import time
from typing import Any

from aiosqlite import Connection, Row

ACTIVE_STATUSES = ("scheduled", "open")


def now() -> int:
    return int(time.time())


# ---------- users ----------

async def upsert_user(conn: Connection, user_id: int, first_name: str,
                      last_name: str | None, username: str | None) -> None:
    ts = now()
    await conn.execute(
        """INSERT INTO users (id, first_name, last_name, username, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT(id) DO UPDATE SET
               first_name = excluded.first_name,
               last_name  = excluded.last_name,
               username   = excluded.username,
               updated_at = excluded.updated_at""",
        (user_id, first_name, last_name, username, ts, ts),
    )


async def set_can_dm(conn: Connection, user_id: int, value: bool) -> None:
    await conn.execute("UPDATE users SET can_dm = ? WHERE id = ?", (int(value), user_id))


async def get_user(conn: Connection, user_id: int) -> Row | None:
    cur = await conn.execute("SELECT * FROM users WHERE id = ?", (user_id,))
    return await cur.fetchone()


# ---------- chats & streams ----------

async def upsert_chat(conn: Connection, chat_id: int, title: str, added_by: int | None) -> None:
    await conn.execute(
        """INSERT INTO chats (id, title, is_active, added_by, created_at) VALUES (?, ?, 1, ?, ?)
           ON CONFLICT(id) DO UPDATE SET title = excluded.title, is_active = 1""",
        (chat_id, title, added_by, now()),
    )


async def set_chat_active(conn: Connection, chat_id: int, active: bool) -> None:
    await conn.execute("UPDATE chats SET is_active = ? WHERE id = ?", (int(active), chat_id))


async def migrate_chat(conn: Connection, old_id: int, new_id: int) -> None:
    """Группа стала супергруппой и получила новый id."""
    cur = await conn.execute("SELECT 1 FROM chats WHERE id = ?", (new_id,))
    if await cur.fetchone() is None:
        # stream_chats и queue_messages обновятся каскадом (ON UPDATE CASCADE)
        await conn.execute("UPDATE chats SET id = ? WHERE id = ?", (new_id, old_id))
        return
    # новый чат уже успели зарегистрировать — переносим потоки и удаляем старый
    await conn.execute(
        """INSERT OR IGNORE INTO stream_chats (stream_id, chat_id)
           SELECT stream_id, ? FROM stream_chats WHERE chat_id = ?""",
        (new_id, old_id),
    )
    await conn.execute("DELETE FROM chats WHERE id = ?", (old_id,))


async def get_chat_streams(conn: Connection, chat_id: int) -> list[Row]:
    cur = await conn.execute(
        """SELECT s.* FROM streams s JOIN stream_chats sc ON sc.stream_id = s.id
           WHERE sc.chat_id = ? ORDER BY s.id""",
        (chat_id,),
    )
    return list(await cur.fetchall())


async def create_stream(conn: Connection, title: str) -> int:
    cur = await conn.execute(
        "INSERT INTO streams (title, created_at) VALUES (?, ?)", (title, now())
    )
    return cur.lastrowid


async def get_stream(conn: Connection, stream_id: int) -> Row | None:
    cur = await conn.execute("SELECT * FROM streams WHERE id = ?", (stream_id,))
    return await cur.fetchone()


async def add_stream_chat(conn: Connection, stream_id: int, chat_id: int) -> None:
    await conn.execute(
        "INSERT OR IGNORE INTO stream_chats (stream_id, chat_id) VALUES (?, ?)", (stream_id, chat_id)
    )


async def get_stream_active_chats(conn: Connection, stream_id: int) -> list[int]:
    cur = await conn.execute(
        """SELECT c.id FROM chats c JOIN stream_chats sc ON sc.chat_id = c.id
           WHERE sc.stream_id = ? AND c.is_active = 1""",
        (stream_id,),
    )
    return [r["id"] for r in await cur.fetchall()]


async def add_stream_admin(conn: Connection, stream_id: int, user_id: int) -> None:
    await conn.execute(
        "INSERT OR IGNORE INTO stream_admins (stream_id, user_id) VALUES (?, ?)", (stream_id, user_id)
    )


async def is_stream_admin(conn: Connection, stream_id: int, user_id: int) -> bool:
    cur = await conn.execute(
        "SELECT 1 FROM stream_admins WHERE stream_id = ? AND user_id = ?", (stream_id, user_id)
    )
    return await cur.fetchone() is not None


async def touch_chat_member(conn: Connection, chat_id: int, user_id: int) -> bool:
    """Запомнить пользователя участником всех потоков чата. False — чат пока ни в одном потоке."""
    cur = await conn.execute(
        """INSERT INTO stream_members (stream_id, user_id, last_seen)
           SELECT stream_id, ?, ? FROM stream_chats WHERE chat_id = ? AND true
           ON CONFLICT(stream_id, user_id) DO UPDATE SET last_seen = excluded.last_seen""",
        (user_id, now(), chat_id),
    )
    return cur.rowcount > 0


async def touch_stream_member(conn: Connection, stream_id: int, user_id: int) -> None:
    await conn.execute(
        """INSERT INTO stream_members (stream_id, user_id, last_seen) VALUES (?, ?, ?)
           ON CONFLICT(stream_id, user_id) DO UPDATE SET last_seen = excluded.last_seen""",
        (stream_id, user_id, now()),
    )


async def get_addable_members(conn: Connection, queue_id: int, exclude_user_id: int) -> list[Row]:
    """Участники потока очереди, которых ещё нет в ней (своей записью)."""
    cur = await conn.execute(
        """SELECT u.* FROM stream_members sm
           JOIN users u ON u.id = sm.user_id
           JOIN queues q ON q.stream_id = sm.stream_id
           WHERE q.id = ? AND u.id != ?
             AND NOT EXISTS (SELECT 1 FROM queue_entries e
                             WHERE e.queue_id = q.id AND e.user_id = u.id AND e.guest_name IS NULL)
           ORDER BY u.first_name, u.last_name""",
        (queue_id, exclude_user_id),
    )
    return list(await cur.fetchall())


async def get_admin_streams(conn: Connection, user_id: int) -> list[Row]:
    cur = await conn.execute(
        """SELECT s.* FROM streams s JOIN stream_admins sa ON sa.stream_id = s.id
           WHERE sa.user_id = ? ORDER BY s.title""",
        (user_id,),
    )
    return list(await cur.fetchall())


# ---------- queues ----------

async def create_queue(conn: Connection, *, stream_id: int, subject: str, starts_at: int,
                       ends_at: int, opens_at: int, status: str, created_by: int | None,
                       schedule_id: int | None = None) -> int:
    cur = await conn.execute(
        """INSERT INTO queues (stream_id, schedule_id, subject, starts_at, ends_at, opens_at,
                               status, created_by, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (stream_id, schedule_id, subject, starts_at, ends_at, opens_at, status, created_by, now()),
    )
    return cur.lastrowid


async def get_queue(conn: Connection, queue_id: int) -> Row | None:
    cur = await conn.execute("SELECT * FROM queues WHERE id = ?", (queue_id,))
    return await cur.fetchone()


async def set_queue_status(conn: Connection, queue_id: int, status: str) -> None:
    await conn.execute("UPDATE queues SET status = ? WHERE id = ?", (status, queue_id))


async def get_active_queues(conn: Connection) -> list[Row]:
    cur = await conn.execute(
        "SELECT * FROM queues WHERE status IN (?, ?) ORDER BY starts_at", ACTIVE_STATUSES
    )
    return list(await cur.fetchall())


# ---------- entries ----------

_ENTRY_SELECT = """
    SELECT e.*, u.first_name, u.last_name, u.username, u.can_dm
    FROM queue_entries e JOIN users u ON u.id = e.user_id
"""


async def get_entries(conn: Connection, queue_id: int) -> list[Row]:
    cur = await conn.execute(_ENTRY_SELECT + " WHERE e.queue_id = ? ORDER BY e.position", (queue_id,))
    return list(await cur.fetchall())


async def get_entry(conn: Connection, entry_id: int) -> Row | None:
    cur = await conn.execute(_ENTRY_SELECT + " WHERE e.id = ?", (entry_id,))
    return await cur.fetchone()


async def find_entry(conn: Connection, queue_id: int, user_id: int, *, guest: bool) -> Row | None:
    cond = "IS NOT NULL" if guest else "IS NULL"
    cur = await conn.execute(
        _ENTRY_SELECT + f" WHERE e.queue_id = ? AND e.user_id = ? AND e.guest_name {cond}",
        (queue_id, user_id),
    )
    return await cur.fetchone()


async def insert_entry(conn: Connection, *, queue_id: int, position: int, user_id: int,
                       guest_name: str | None, added_by: int) -> int:
    cur = await conn.execute(
        """INSERT INTO queue_entries (queue_id, position, user_id, guest_name, added_by, created_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (queue_id, position, user_id, guest_name, added_by, now()),
    )
    return cur.lastrowid


async def delete_entry(conn: Connection, entry_id: int) -> None:
    await conn.execute("DELETE FROM queue_entries WHERE id = ?", (entry_id,))


async def set_positions(conn: Connection, positions: list[tuple[int, int]]) -> None:
    """positions: [(entry_id, position), ...]"""
    await conn.executemany(
        "UPDATE queue_entries SET position = ? WHERE id = ?", [(p, e) for e, p in positions]
    )


async def count_added_others(conn: Connection, queue_id: int, user_id: int) -> int:
    """Сколько людей (кроме себя) пользователь записал в очередь."""
    cur = await conn.execute(
        """SELECT COUNT(*) FROM queue_entries
           WHERE queue_id = ? AND added_by = ? AND (user_id != ? OR guest_name IS NOT NULL)""",
        (queue_id, user_id, user_id),
    )
    return (await cur.fetchone())[0]


async def get_user_active_entries(conn: Connection, user_id: int) -> list[Row]:
    """Записи пользователя и записи, которые он сделал за других, в активных очередях."""
    cur = await conn.execute(
        """SELECT e.id AS entry_id, e.position, e.guest_name, e.user_id, e.added_by,
                  u.first_name, u.last_name,
                  a.first_name AS adder_first_name, a.last_name AS adder_last_name,
                  q.id AS queue_id, q.subject, q.starts_at, q.ends_at, q.opens_at, q.status,
                  (SELECT COUNT(*) FROM queue_entries x WHERE x.queue_id = q.id) AS total
           FROM queue_entries e
           JOIN queues q ON q.id = e.queue_id
           JOIN users u ON u.id = e.user_id
           JOIN users a ON a.id = e.added_by
           WHERE (e.user_id = ? OR e.added_by = ?) AND q.status IN (?, ?)
           ORDER BY q.starts_at, q.id, e.position""",
        (user_id, user_id, *ACTIVE_STATUSES),
    )
    return list(await cur.fetchall())


async def get_users(conn: Connection, user_ids: list[int]) -> dict[int, Row]:
    if not user_ids:
        return {}
    marks = ",".join("?" * len(user_ids))
    cur = await conn.execute(f"SELECT * FROM users WHERE id IN ({marks})", list(user_ids))
    return {r["id"]: r for r in await cur.fetchall()}


# ---------- swap requests ----------

async def create_swap_request(conn: Connection, queue_id: int, from_entry_id: int, to_entry_id: int) -> int:
    cur = await conn.execute(
        """INSERT INTO swap_requests (queue_id, from_entry_id, to_entry_id, status, created_at)
           VALUES (?, ?, ?, 'pending', ?)""",
        (queue_id, from_entry_id, to_entry_id, now()),
    )
    return cur.lastrowid


async def get_swap_request(conn: Connection, request_id: int) -> Row | None:
    cur = await conn.execute("SELECT * FROM swap_requests WHERE id = ?", (request_id,))
    return await cur.fetchone()


async def set_swap_status(conn: Connection, request_id: int, status: str) -> None:
    await conn.execute("UPDATE swap_requests SET status = ? WHERE id = ?", (status, request_id))


async def cancel_pending_swaps_from(conn: Connection, from_entry_id: int) -> None:
    await conn.execute(
        "UPDATE swap_requests SET status = 'cancelled' WHERE from_entry_id = ? AND status = 'pending'",
        (from_entry_id,),
    )


async def expire_swaps_for_entries(conn: Connection, entry_ids: list[int]) -> None:
    if not entry_ids:
        return
    marks = ",".join("?" * len(entry_ids))
    await conn.execute(
        f"""UPDATE swap_requests SET status = 'expired'
            WHERE status = 'pending' AND (from_entry_id IN ({marks}) OR to_entry_id IN ({marks}))""",
        [*entry_ids, *entry_ids],
    )


async def expire_queue_swaps(conn: Connection, queue_id: int) -> None:
    await conn.execute(
        "UPDATE swap_requests SET status = 'expired' WHERE queue_id = ? AND status = 'pending'", (queue_id,)
    )


# ---------- messages ----------

async def save_queue_message(conn: Connection, queue_id: int, chat_id: int, message_id: int) -> None:
    await conn.execute(
        "INSERT OR REPLACE INTO queue_messages (queue_id, chat_id, message_id) VALUES (?, ?, ?)",
        (queue_id, chat_id, message_id),
    )


async def get_queue_messages(conn: Connection, queue_id: int) -> list[Row]:
    cur = await conn.execute("SELECT * FROM queue_messages WHERE queue_id = ?", (queue_id,))
    return list(await cur.fetchall())


async def delete_queue_message(conn: Connection, queue_id: int, chat_id: int) -> None:
    await conn.execute(
        "DELETE FROM queue_messages WHERE queue_id = ? AND chat_id = ?", (queue_id, chat_id)
    )


# ---------- events ----------

async def log_event(conn: Connection, queue_id: int, actor_id: int | None, type_: str,
                    payload: dict[str, Any] | None = None) -> None:
    await conn.execute(
        "INSERT INTO queue_events (queue_id, actor_id, type, payload, created_at) VALUES (?, ?, ?, ?, ?)",
        (queue_id, actor_id, type_,
         json.dumps(payload, ensure_ascii=False) if payload is not None else None, now()),
    )
