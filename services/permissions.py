"""Кто что может. Единственное место проверки прав (точка расширения для модерации)."""
from enum import StrEnum

from aiosqlite import Connection, Row

from db import repo


class Action(StrEnum):
    CREATE_QUEUE = "create_queue"
    DELETE_QUEUE = "delete_queue"
    JOIN = "join"
    LEAVE = "leave"    # убрать свою запись или запись, которую сделал сам
    SWAP = "swap"
    REMOVE = "remove"  # убрать чужую запись — этап 5
    MOVE = "move"      # переставить чужую запись — этап 5


async def can(conn: Connection, user_id: int, action: Action, *,
              stream_id: int | None = None, entry: Row | None = None) -> bool:
    match action:
        case Action.CREATE_QUEUE | Action.DELETE_QUEUE:
            return stream_id is not None and await repo.is_stream_admin(conn, stream_id, user_id)
        case Action.JOIN | Action.SWAP:
            return True
        case Action.LEAVE:
            return entry is not None and user_id in (entry["user_id"], entry["added_by"])
        case Action.REMOVE | Action.MOVE:
            # Этап 5: return stream_id is not None and await repo.is_stream_admin(conn, stream_id, user_id)
            return False
    return False
