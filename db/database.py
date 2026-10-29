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
