import pytest

from db import repo
from services.notifier import Change, compute_changes
from services.queue_service import QueueError, SWAP_REQUEST_TTL


async def order(db, queue_id):
    async with db.read() as conn:
        return [e["user_id"] for e in await repo.get_entries(conn, queue_id)]


async def swap_status(db, request_id):
    async with db.read() as conn:
        return (await repo.get_swap_request(conn, request_id))["status"]


async def fill(service, q, users):
    return {uid: (await service.join(q, uid)).entry_id for uid in users}


# ---------- обмен ----------

async def test_swap_accept(env):
    db, service, _, q = env
    e = await fill(service, q, [2, 3, 4, 5])
    rid = await service.request_swap(e[2], e[4], actor_id=2)
    res = await service.accept_swap(rid, actor_id=4)
    assert await order(db, q) == [4, 3, 2, 5]
    assert res.kind == "swap" and res.position == 3
    assert await swap_status(db, rid) == "accepted"
    with pytest.raises(QueueError, match="неактуален"):  # второй раз принять нельзя
        await service.accept_swap(rid, actor_id=4)


async def test_swap_permissions(env):
    db, service, _, q = env
    e = await fill(service, q, [2, 3, 4])
    with pytest.raises(QueueError):  # чужим местом меняться нельзя
        await service.request_swap(e[3], e[4], actor_id=2)
    with pytest.raises(QueueError):  # с самим собой нельзя
        await service.request_swap(e[2], e[2], actor_id=2)
    rid = await service.request_swap(e[2], e[3], actor_id=2)
    with pytest.raises(QueueError):  # принять может только адресат
        await service.accept_swap(rid, actor_id=4)
    with pytest.raises(QueueError):  # отменить может только инициатор
        await service.cancel_swap(rid, actor_id=3)
    await service.decline_swap(rid, actor_id=3)
    assert await swap_status(db, rid) == "declined"
    assert await order(db, q) == [2, 3, 4]


async def test_swap_requires_dm(env):
    db, service, _, q = env
    e = await fill(service, q, [2, 3])
    async with db.transaction() as conn:
        await repo.set_can_dm(conn, 3, False)
    with pytest.raises(QueueError, match="личка"):
        await service.request_swap(e[2], e[3], actor_id=2)


async def test_swap_expires_when_positions_shift(env):
    db, service, _, q = env
    e = await fill(service, q, [2, 3, 4, 5])
    rid = await service.request_swap(e[3], e[5], actor_id=3)
    untouched = await service.request_swap(e[2], e[3], actor_id=2)
    await service.leave(e[4], actor_id=4)  # 5-й сдвинулся вверх → запрос 3↔5 неактуален
    assert await swap_status(db, rid) == "expired"
    assert await swap_status(db, untouched) == "pending"  # места 2 и 3 не сдвинулись
    with pytest.raises(QueueError):
        await service.accept_swap(rid, actor_id=5)


async def test_swap_request_removed_with_entry(env):
    db, service, _, q = env
    e = await fill(service, q, [2, 3])
    rid = await service.request_swap(e[2], e[3], actor_id=2)
    await service.leave(e[2], actor_id=2)
    with pytest.raises(QueueError, match="неактуален"):
        await service.accept_swap(rid, actor_id=3)


async def test_swap_ttl_and_replace(env):
    db, service, _, q = env
    e = await fill(service, q, [2, 3, 4])
    first = await service.request_swap(e[4], e[2], actor_id=4)
    second = await service.request_swap(e[4], e[3], actor_id=4)  # новый запрос отменяет старый
    assert await swap_status(db, first) == "cancelled"
    async with db.transaction() as conn:
        await conn.execute("UPDATE swap_requests SET created_at = created_at - ? WHERE id = ?",
                           (SWAP_REQUEST_TTL + 1, second))
    with pytest.raises(QueueError, match="устарел"):
        await service.accept_swap(second, actor_id=3)
    assert await swap_status(db, second) == "expired"


async def test_swap_closed_queue(env):
    db, service, _, q = env
    e = await fill(service, q, [2, 3])
    rid = await service.request_swap(e[2], e[3], actor_id=2)
    await service.close_queue(q)
    assert await swap_status(db, rid) == "expired"


async def test_swap_name_entry_goes_to_its_owner(env):
    db, service, _, q = env
    e = await fill(service, q, [2])
    named = await service.add_by_name(q, 3, "Вася")  # владелец — 3
    rid = await service.request_swap(named.entry_id, e[2], actor_id=3)
    await service.accept_swap(rid, actor_id=2)
    async with db.read() as conn:
        assert [x["guest_name"] for x in await repo.get_entries(conn, q)] == ["Вася", None]


# ---------- уведомления ----------

async def test_notify_on_leave(env):
    _, service, _, q = env
    e = await fill(service, q, [2, 3, 4, 5])
    res = await service.leave(e[3], actor_id=3)
    changes = compute_changes(res)
    assert changes == [Change(4, e[4], None, 3, 2), Change(5, e[5], None, 4, 3)]  # только те, кто ниже


async def test_notify_on_swap_skips_actor(env):
    _, service, _, q = env
    e = await fill(service, q, [2, 3, 4])
    rid = await service.request_swap(e[2], e[4], actor_id=2)
    res = await service.accept_swap(rid, actor_id=4)
    assert compute_changes(res) == [Change(2, e[2], None, 1, 3)]  # 3-й не сдвинулся, 4-й — инициатор


async def test_notify_removed_by_recorder(env):
    _, service, _, q = env
    res = await service.add_classmate(q, actor_id=2, classmate_id=5)
    other = await service.join(q, 6)
    removed = await service.leave(res.entry_id, actor_id=2)
    assert compute_changes(removed) == [
        Change(5, res.entry_id, None, 1, None),  # одногруппнику: тебя убрали
        Change(6, other.entry_id, None, 2, 1),
    ]


async def test_notify_name_entry_goes_to_owner(env):
    _, service, _, q = env
    e = await fill(service, q, [2])
    named = await service.add_by_name(q, 3, "Вася")
    res = await service.leave(e[2], actor_id=2)
    assert compute_changes(res) == [Change(3, named.entry_id, "Вася", 2, 1)]


async def test_no_notifications_on_join(env):
    _, service, _, q = env
    await fill(service, q, [2])
    assert compute_changes(await service.join(q, 3)) == []
