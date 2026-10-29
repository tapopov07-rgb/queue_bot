from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import Chat, TelegramObject, User

from db import repo
from db.database import Database

GROUPS = {"group", "supergroup"}


class UserMiddleware(BaseMiddleware):
    """При любом апдейте сохраняет пользователя, а в группах — запоминает его участником
    потоков этого чата (из них строится список «Записать одногруппника»).
    В БД пишет только при изменениях."""

    def __init__(self) -> None:
        self._seen_users: dict[int, tuple] = {}
        self._seen_members: set[tuple[int, int]] = set()

    async def __call__(self, handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
                       event: TelegramObject, data: dict[str, Any]) -> Any:
        user: User | None = data.get("event_from_user")
        chat: Chat | None = data.get("event_chat")
        if user and not user.is_bot:
            key = (user.first_name, user.last_name, user.username)
            new_user = self._seen_users.get(user.id) != key
            new_member = chat is not None and chat.type in GROUPS and (chat.id, user.id) not in self._seen_members
            if new_user or new_member:
                db: Database = data["db"]
                async with db.transaction() as conn:
                    if new_user:
                        await repo.upsert_user(conn, user.id, user.first_name, user.last_name, user.username)
                    if new_member and await repo.touch_chat_member(conn, chat.id, user.id):
                        self._seen_members.add((chat.id, user.id))
                self._seen_users[user.id] = key
        return await handler(event, data)
