"""Открытие и закрытие очередей по времени. Задачи хранятся в памяти и
восстанавливаются из БД при каждом старте, поэтому перезапуск ничего не теряет."""
import logging
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from aiosqlite import Row

from db import repo
from db.database import Database
from services.queue_service import QueueService
from services.renderer import QueueMessenger

log = logging.getLogger(__name__)


class QueueScheduler:
    def __init__(self, db: Database, service: QueueService, messenger: QueueMessenger, tz: ZoneInfo) -> None:
        self.db = db
        self.service = service
        self.messenger = messenger
        self.tz = tz
        self.scheduler = AsyncIOScheduler(timezone=tz)

    async def start(self) -> None:
        self.scheduler.start()
        async with self.db.read() as conn:
            queues = await repo.get_active_queues(conn)
        for q in queues:
            self.plan(q)
        log.info("Планировщик запущен, активных очередей: %d", len(queues))

    def shutdown(self) -> None:
        self.scheduler.shutdown(wait=False)

    def plan(self, queue: Row) -> None:
        """Поставить задачи открытия/закрытия. Просроченные выполняются сразу."""
        qid = queue["id"]
        now = time.time()
        if queue["status"] == "scheduled" and queue["ends_at"] > now:
            self._add(f"open:{qid}", self._open, qid, max(queue["opens_at"], now + 1))
        self._add(f"close:{qid}", self._close, qid, max(queue["ends_at"], now + 2))

    def _add(self, job_id: str, func, queue_id: int, ts: float) -> None:
        self.scheduler.add_job(
            func, "date", run_date=datetime.fromtimestamp(ts, self.tz), args=[queue_id],
            id=job_id, replace_existing=True, misfire_grace_time=None,
        )

    async def _open(self, queue_id: int) -> None:
        if await self.service.open_queue(queue_id):
            log.info("Очередь %s открыта", queue_id)
            self.messenger.refresh(queue_id)

    async def _close(self, queue_id: int) -> None:
        if await self.service.close_queue(queue_id):
            log.info("Очередь %s закрыта", queue_id)
            self.messenger.refresh(queue_id)
