import pytest

from db import repo
from db.database import Database
from services.queue_service import QueueService

ADMIN = 1


@pytest.fixture
async def env(tmp_path):
    db = Database(tmp_path / "test.db")
    await db.init()
    async with db.transaction() as conn:
        for uid in range(1, 31):
            await repo.upsert_user(conn, uid, f"User{uid}", None, None)
            await repo.set_can_dm(conn, uid, True)
        stream_id = await repo.create_stream(conn, "Тест")
        await repo.add_stream_admin(conn, stream_id, ADMIN)
    service = QueueService(db)
    now = repo.now()
    queue_id = await service.create_queue(
        stream_id=stream_id, subject="Матан", starts_at=now + 3600, ends_at=now + 9000,
        opens_at=now - 1, actor_id=ADMIN,
    )
    return db, service, stream_id, queue_id


