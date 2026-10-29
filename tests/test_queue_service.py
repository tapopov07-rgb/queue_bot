import asyncio

import pytest

from db import repo
from services.queue_service import QueueError, QueueService

from tests.conftest import ADMIN


async def positions(db, queue_id):
    async with db.read() as conn:
        return [(e["user_id"], e["guest_name"], e["position"]) for e in await repo.get_entries(conn, queue_id)]


async def test_join_order_and_double_join(env):
    db, service, _, q = env
    assert (await service.join(q, 2)).position == 1
    assert (await service.join(q, 3)).position == 2
    with pytest.raises(QueueError):
        await service.join(q, 2)


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
    assert (await service.join(q2, 2)).position == 1
    assert await service.close_queue(q2)
    with pytest.raises(QueueError, match="закрыта"):
        await service.join(q2, 3)


async def test_add_by_name_limit_and_leave_repositions(env):
    db, service, _, q = env
    await service.join(q, 2)
    named = await service.add_by_name(q, 2, "  Вася   Пупкин ")
    await service.join(q, 3)
    with pytest.raises(QueueError):
        await service.add_by_name(q, 2, "Петя")
    assert await positions(db, q) == [(2, None, 1), (2, "Вася Пупкин", 2), (3, None, 3)]

    res = await service.leave(named.entry_id, actor_id=2)
    assert await positions(db, q) == [(2, None, 1), (3, None, 2)]
    assert res.before != res.after


async def test_add_classmate_ownership_and_limit(env):
    db, service, _, q = env
    res = await service.add_classmate(q, actor_id=2, classmate_id=5)
    assert await positions(db, q) == [(5, None, 1)]  # запись принадлежит одногруппнику

    # общий лимит: после одногруппника нельзя ни ещё одного, ни по имени
    with pytest.raises(QueueError):
        await service.add_classmate(q, actor_id=2, classmate_id=6)
    with pytest.raises(QueueError):
        await service.add_by_name(q, 2, "Петя")
    # одногруппник уже в очереди — сам записаться повторно не может
    with pytest.raises(QueueError):
        await service.join(q, 5)
    # себя через «одногруппника» нельзя
    with pytest.raises(QueueError):
        await service.add_classmate(q, actor_id=3, classmate_id=3)
    # посторонний убрать не может
    with pytest.raises(QueueError):
        await service.leave(res.entry_id, actor_id=4)
    # убрать может сам одногруппник...
    await service.leave(res.entry_id, actor_id=5)
    assert await positions(db, q) == []
    # ...и тогда лимит записавшего освобождается; убрать может и записавший
    res2 = await service.add_classmate(q, actor_id=2, classmate_id=6)
    await service.leave(res2.entry_id, actor_id=2)
    assert await positions(db, q) == []


async def test_addable_members(env):
    db, service, stream_id, q = env
    async with db.transaction() as conn:
        for uid in (2, 3, 4):
            await repo.touch_stream_member(conn, stream_id, uid)
    await service.join(q, 3)
    async with db.read() as conn:
        members = await repo.get_addable_members(conn, q, exclude_user_id=2)
    assert [m["id"] for m in members] == [4]  # без себя и без тех, кто уже в очереди


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
    assert await positions(db, q) == [(2, "Гость", 1)]
    await service.leave_queue(q, 2)
    assert await positions(db, q) == []
    with pytest.raises(QueueError):
        await service.leave_queue(q, 2)


async def test_concurrent_joins_get_unique_positions(env):
    db, service, _, q = env
    await asyncio.gather(*(service.join(q, uid) for uid in range(2, 22)))
    pos = [p for _, _, p in await positions(db, q)]
    assert pos == list(range(1, 21))


async def test_events_logged(env):
    db, service, _, q = env
    e = await service.join(q, 2)
    await service.leave(e.entry_id, 2)
    async with db.read() as conn:
        cur = await conn.execute("SELECT type FROM queue_events WHERE queue_id = ? ORDER BY id", (q,))
        assert [r["type"] for r in await cur.fetchall()] == ["create", "join", "leave"]


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
