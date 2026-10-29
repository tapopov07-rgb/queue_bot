import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import aiosqlite

SCHEMA_PATH = Path(__file__).with_name("schema.sql")


class Database:
    """Каждая операция открывает своё соединение: чтения изолированы от
    незакоммиченных записей, а все записи идут строго по одной (write lock),
    поэтому одновременные нажатия кнопок не ломают порядок в очереди."""

    def __init__(self, path: Path | str) -> None:
        self.path = path
        self._write_lock = asyncio.Lock()

    async def _open(self) -> aiosqlite.Connection:
        conn = await aiosqlite.connect(self.path, isolation_level=None)
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA foreign_keys = ON")
        await conn.execute("PRAGMA busy_timeout = 5000")
        return conn

    async def init(self) -> None:
        conn = await self._open()
        try:
            await conn.execute("PRAGMA journal_mode = WAL")
            await _migrate(conn)
            await conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        finally:
            await conn.close()

    @asynccontextmanager
    async def read(self) -> AsyncIterator[aiosqlite.Connection]:
        conn = await self._open()
        try:
            yield conn
        finally:
            await conn.close()

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[aiosqlite.Connection]:
        async with self._write_lock:
            conn = await self._open()
            try:
                await conn.execute("BEGIN IMMEDIATE")
                try:
                    yield conn
                except BaseException:
                    await conn.execute("ROLLBACK")
                    raise
                await conn.execute("COMMIT")
            finally:
                await conn.close()


async def _columns(conn: aiosqlite.Connection, table: str) -> set[str]:
    cur = await conn.execute(f"PRAGMA table_info({table})")
    return {row[1] for row in await cur.fetchall()}


async def _migrate(conn: aiosqlite.Connection) -> None:
    """Обновить базу, созданную старой версией бота. На новой базе ничего не делает."""
    if "position" in await _columns(conn, "queue_entries"):
        # места стали листом с номерами: старые позиции 1..N становятся номерами мест
        await conn.execute("ALTER TABLE queue_entries RENAME COLUMN position TO slot")
    queue_cols = await _columns(conn, "queues")
    if queue_cols and "capacity" not in queue_cols:
        await conn.execute("ALTER TABLE queues ADD COLUMN capacity INTEGER NOT NULL DEFAULT 30")
    if "weekday" in await _columns(conn, "schedule"):
        # черновая таблица расписания из первой версии — не использовалась, заменяется новой
        await conn.execute("UPDATE queues SET schedule_id = NULL WHERE schedule_id IS NOT NULL")
        await conn.execute("DROP TABLE schedule")
