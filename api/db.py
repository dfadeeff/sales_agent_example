import aiosqlite
from contextlib import asynccontextmanager
from typing import AsyncIterator
from api.config import settings

_db: aiosqlite.Connection | None = None


async def init_db() -> None:
    global _db
    # isolation_level=None → autocommit. All transactions are managed explicitly
    # with BEGIN/COMMIT/ROLLBACK. This prevents Python's sqlite3 from holding an
    # implicit open transaction when MCP write tools need to acquire a write lock.
    _db = await aiosqlite.connect(settings.db_path, isolation_level=None)
    _db.row_factory = aiosqlite.Row
    await _db.execute("PRAGMA journal_mode=WAL")
    await _db.execute("PRAGMA foreign_keys=ON")


async def close_db() -> None:
    global _db
    if _db:
        await _db.close()
        _db = None


def get_db() -> aiosqlite.Connection:
    if _db is None:
        raise RuntimeError("Database not initialised — call init_db() first")
    return _db
