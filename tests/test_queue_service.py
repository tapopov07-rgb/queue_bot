import asyncio
import sqlite3
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from db import repo
from db.database import Database
from services.queue_service import QueueError, queue_window
from tests.conftest import ADMIN


async def slots(db, queue_id):
    """[(user_id, guest_name, slot), ...] по возрастанию номера места."""
    async with db.read() as conn:
        return [(e["user_id"], e["guest_name"], e["slot"]) for e in await repo.get_entries(conn, queue_id)]


async def test_join_takes_first_free_slot(env):
    db, service, _, q = env
    assert (await service.join(q, 2)).slot == 1
    assert (await service.join(q, 3)).slot == 2
    with pytest.raises(QueueError):
        await service.join(q, 2)


async def test_choose_slot(env):
    db, service, _, q = env
    res = await service.join(q, 2, slot=6)
    assert (res.slot, res.position) == (6, 1)
    assert (await service.join(q, 3)).slot == 1          # первое свободное — впереди
    with pytest.raises(QueueError, match="занято"):
        await service.join(q, 4, slot=6)
    for bad in (0, 31):
        with pytest.raises(QueueError, match="от 1 до 30"):
            await service.join(q, 4, slot=bad)
    assert await slots(db, q) == [(3, None, 1), (2, None, 6)]


async def test_capacity_full(env):
    _, service, stream_id, _ = env
    now = repo.now()
    q = await service.create_queue(stream_id=stream_id, subject="X", starts_at=now + 60, ends_at=now + 120,
                                   opens_at=now - 1, capacity=2, actor_id=ADMIN)
    await service.join(q, 2)
    await service.join(q, 3)
    with pytest.raises(QueueError, match="Свободных мест нет"):
        await service.join(q, 4)


async def test_leave_keeps_other_slots(env):
    db, service, _, q = env
    await service.join(q, 2)
    e3 = await service.join(q, 3)
    await service.join(q, 4)
    await service.leave(e3.entry_id, actor_id=3)
    assert await slots(db, q) == [(2, None, 1), (4, None, 3)]   # место 2 пустует, никто не сдвинулся
    assert (await service.join(q, 5)).slot == 2                # и его займёт следующий


async def test_move(env):
    db, service, _, q = env
    a = await service.join(q, 2)
    await service.join(q, 3)
    res = await service.move(a.entry_id, 5, actor_id=2)
    assert (res.slot, res.position) == (5, 2)
    assert await slots(db, q) == [(3, None, 2), (2, None, 5)]
    with pytest.raises(QueueError, match="занято"):
        await service.move(a.entry_id, 2, actor_id=2)
    with pytest.raises(QueueError, match="уже на месте"):
        await service.move(a.entry_id, 5, actor_id=2)
    with pytest.raises(QueueError):  # чужую запись двигать нельзя
        await service.move(a.entry_id, 7, actor_id=3)
    assert (await service.move(a.entry_id, None, actor_id=2)).slot == 1  # «первое свободное»


async def test_only_admin_creates(env):
    _, service, stream_id, _ = env
    now = repo.now()
    with pytest.raises(QueueError):
        await service.create_queue(stream_id=stream_id, subject="X", starts_at=now + 60,
                                   ends_at=now + 120, opens_at=now, actor_id=5)


async def test_scheduled_queue_rejects_join(env):
    _, service, stream_id, _ = env
    now = repo.now()
    q2 = await service.create_queue(stream_id=stream_id, subject="X", starts_at=now + 7200,
                                    ends_at=now + 9000, opens_at=now + 3600, actor_id=ADMIN)
    with pytest.raises(QueueError, match="ещё не открыта"):
        await service.join(q2, 2)
    assert await service.open_queue(q2)
    assert (await service.join(q2, 2)).slot == 1
    assert await service.close_queue(q2)
    with pytest.raises(QueueError, match="закрыта"):
        await service.join(q2, 3)


def test_queue_window():
    tz = ZoneInfo("Europe/Moscow")
    opens, starts, ends = queue_window(date(2026, 11, 4), tz)  # пара в среду
    assert datetime.fromtimestamp(opens, tz) == datetime(2026, 11, 3, 0, 0, tzinfo=tz)      # вт 00:00
    assert datetime.fromtimestamp(starts, tz) == datetime(2026, 11, 4, 0, 0, tzinfo=tz)
    assert datetime.fromtimestamp(ends, tz) == datetime(2026, 11, 4, 23, 59, 59, tzinfo=tz)  # ср 23:59


async def test_add_by_name_limit(env):
    db, service, _, q = env
    await service.join(q, 2)
    named = await service.add_by_name(q, 2, "  Вася   Пупкин ")
    with pytest.raises(QueueError):
        await service.add_by_name(q, 2, "Петя")
    assert await slots(db, q) == [(2, None, 1), (2, "Вася Пупкин", 2)]
    await service.leave(named.entry_id, actor_id=2)
    assert await slots(db, q) == [(2, None, 1)]


async def test_add_classmate_ownership_and_limit(env):
    db, service, _, q = env
    res = await service.add_classmate(q, actor_id=2, classmate_id=5)
    assert await slots(db, q) == [(5, None, 1)]  # запись принадлежит одногруппнику

    with pytest.raises(QueueError):  # общий лимит: одного человека
        await service.add_classmate(q, actor_id=2, classmate_id=6)
    with pytest.raises(QueueError):
        await service.add_by_name(q, 2, "Петя")
    with pytest.raises(QueueError):  # одногруппник уже в очереди
        await service.join(q, 5)
    with pytest.raises(QueueError):  # себя через «одногруппника» нельзя
        await service.add_classmate(q, actor_id=3, classmate_id=3)
    with pytest.raises(QueueError):  # посторонний убрать не может
        await service.leave(res.entry_id, actor_id=4)
    await service.leave(res.entry_id, actor_id=5)  # убрать может сам одногруппник...
    res2 = await service.add_classmate(q, actor_id=2, classmate_id=6)
    await service.leave(res2.entry_id, actor_id=2)  # ...и тот, кто записал
    assert await slots(db, q) == []


async def test_addable_members(env):
    db, service, stream_id, q = env
    async with db.transaction() as conn:
        for uid in (2, 3, 4):
            await repo.touch_stream_member(conn, stream_id, uid)
    await service.join(q, 3)
    async with db.read() as conn:
        members = await repo.get_addable_members(conn, q, exclude_user_id=2)
    assert [m["id"] for m in members] == [4]


async def test_cannot_remove_foreign_entry(env):
    _, service, _, q = env
    entry = await service.join(q, 2)
    with pytest.raises(QueueError):
        await service.leave(entry.entry_id, actor_id=3)
    with pytest.raises(QueueError):  # модерация выключена даже для ответственного (этап 5)
        await service.leave(entry.entry_id, actor_id=ADMIN)


async def test_leave_queue_prefers_own_entry(env):
    db, service, _, q = env
    await service.add_by_name(q, 2, "Гость")
    await service.join(q, 2)
    await service.leave_queue(q, 2)
    assert await slots(db, q) == [(2, "Гость", 1)]
    await service.leave_queue(q, 2)
    assert await slots(db, q) == []
    with pytest.raises(QueueError):
        await service.leave_queue(q, 2)


async def test_concurrent_joins_get_unique_slots(env):
    db, service, _, q = env
    await asyncio.gather(*(service.join(q, uid) for uid in range(2, 22)))
    assert [s for _, _, s in await slots(db, q)] == list(range(1, 21))


async def test_concurrent_same_slot(env):
    db, service, _, q = env
    results = await asyncio.gather(*(service.join(q, uid, slot=7) for uid in range(2, 12)),
                                    return_exceptions=True)
    assert sum(not isinstance(r, Exception) for r in results) == 1  # место 7 достанется одному
    assert [s for _, _, s in await slots(db, q)] == [7]


async def test_events_logged(env):
    db, service, _, q = env
    e = await service.join(q, 2)
    await service.move(e.entry_id, 4, 2)
    await service.leave(e.entry_id, 2)
    async with db.read() as conn:
        cur = await conn.execute("SELECT type FROM queue_events WHERE queue_id = ? ORDER BY id", (q,))
        assert [r["type"] for r in await cur.fetchall()] == ["create", "join", "move", "leave"]


async def test_delete_queue(env):
    db, service, _, q = env
    a = await service.join(q, 2)
    b = await service.join(q, 3)
    await service.request_swap(a.entry_id, b.entry_id, actor_id=2)
    with pytest.raises(QueueError):  # не ответственный
        await service.delete_queue(q, actor_id=2)
    queue, _ = await service.delete_queue(q, actor_id=ADMIN)
    assert queue["subject"] == "Матан"
    async with db.read() as conn:
        assert await repo.get_queue(conn, q) is None
        assert await repo.get_user_active_entries(conn, 2) == []
        for table in ("queue_entries", "queue_events", "swap_requests"):
            cur = await conn.execute(f"SELECT COUNT(*) FROM {table}")
            assert (await cur.fetchone())[0] == 0, table
    with pytest.raises(QueueError):
        await service.delete_queue(q, actor_id=ADMIN)


async def test_my_queues_rank_and_ahead(env):
    db, service, _, q = env
    await service.join(q, 2, slot=2)
    await service.add_by_name(q, 3, "Вася")  # первое свободное — место 1
    await service.join(q, 4, slot=6)
    async with db.read() as conn:
        rows = await repo.get_user_active_entries(conn, 4)
    (row,) = rows
    assert (row["slot"], row["rank"], row["total"], row["prev_first_name"]) == (6, 3, 3, "User2")


async def test_migration_from_position_schema(tmp_path):
    """База старой версии (колонка position, без capacity) обновляется без потери записей."""
    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE users (id INTEGER PRIMARY KEY, first_name TEXT NOT NULL, last_name TEXT, username TEXT,
                            can_dm INTEGER NOT NULL DEFAULT 0, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL);
        CREATE TABLE queues (id INTEGER PRIMARY KEY AUTOINCREMENT, stream_id INTEGER NOT NULL, schedule_id INTEGER,
                             subject TEXT NOT NULL, starts_at INTEGER NOT NULL, ends_at INTEGER NOT NULL,
                             opens_at INTEGER NOT NULL, status TEXT NOT NULL, created_by INTEGER, created_at INTEGER NOT NULL);
        CREATE TABLE queue_entries (id INTEGER PRIMARY KEY AUTOINCREMENT, queue_id INTEGER NOT NULL,
                                    position INTEGER NOT NULL, user_id INTEGER NOT NULL, guest_name TEXT,
                                    added_by INTEGER NOT NULL, created_at INTEGER NOT NULL);
        INSERT INTO users VALUES (1, 'A', NULL, NULL, 1, 0, 0), (2, 'B', NULL, NULL, 1, 0, 0);
        INSERT INTO queues VALUES (1, 1, NULL, 'Old', 0, 9999999999, 0, 'open', 1, 0);
        INSERT INTO queue_entries VALUES (1, 1, 1, 1, NULL, 1, 0), (2, 1, 2, 2, NULL, 2, 0);
    """)
    con.close()

    db = Database(path)
    await db.init()
    async with db.read() as conn:
        assert [(e["user_id"], e["slot"]) for e in await repo.get_entries(conn, 1)] == [(1, 1), (2, 2)]
        assert (await repo.get_queue(conn, 1))["capacity"] == 30
    await db.init()  # повторный запуск ничего не ломает
