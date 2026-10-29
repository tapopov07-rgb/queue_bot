"""Уведомления о сдвиге мест.

Получатели вычисляются по разнице позиций «до/после» (compute_changes), а не по типу
операции: любая новая операция (в т.ч. модерация на этапе 5) автоматически уведомит
всех, чьё место изменилось. Уведомление получает владелец записи (user_id).
"""
import asyncio
import logging
from collections import defaultdict
from dataclasses import dataclass
from html import escape
from zoneinfo import ZoneInfo

from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from aiosqlite import Row

from db import repo
from db.database import Database
from services.queue_service import EntrySnap, OpResult
from services.renderer import fmt_class_day, user_name

log = logging.getLogger(__name__)

SEND_INTERVAL = 0.05  # не больше ~20 сообщений в секунду (лимит Telegram — 30)


@dataclass(frozen=True)
class Change:
    user_id: int            # кому отправить
    entry_id: int
    guest_name: str | None  # место человека, вписанного по имени
    old: int
    new: int | None         # None — запись убрана


def compute_changes(result: OpResult) -> list[Change]:
    """Кого уведомить: всех, чьё место сдвинулось или чью запись убрал кто-то другой.
    Инициатор действия сам знает, что сделал, — его не уведомляем."""
    changes = []
    for entry_id, b in result.before.items():
        if b.user_id == result.actor_id:
            continue
        a = result.after.get(entry_id)
        if a is None or a.position != b.position:
            changes.append(Change(b.user_id, entry_id, b.guest_name, b.position, a.position if a else None))
    return changes


class Notifier:
    def __init__(self, bot: Bot, db: Database, tz: ZoneInfo) -> None:
        self.bot = bot
        self.db = db
        self.tz = tz
        self._tasks: set[asyncio.Task] = set()

    def notify(self, result: OpResult) -> None:
        """Разослать уведомления в фоне — хендлер отвечает пользователю сразу."""
        if not compute_changes(result):
            return
        task = asyncio.create_task(self._send(result))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _send(self, result: OpResult) -> None:
        try:
            changes = compute_changes(result)
            ids = {c.user_id for c in changes} | {s.user_id for s in result.before.values()}
            if result.actor_id:
                ids.add(result.actor_id)
            async with self.db.read() as conn:
                queue = await repo.get_queue(conn, result.queue_id)
                users = await repo.get_users(conn, list(ids))

            by_user: dict[int, list[Change]] = defaultdict(list)
            for c in changes:
                by_user[c.user_id].append(c)

            header = f"📋 <b>{escape(queue['subject'])}</b> · {fmt_class_day(queue, self.tz)}"
            reason = self._reason(result, users)
            for user_id, user_changes in by_user.items():
                user = users.get(user_id)
                if user is None or not user["can_dm"]:
                    continue
                lines = [header, reason, ""] + [self._line(c, result, users) for c in user_changes]
                await self._deliver(user_id, "\n".join(lines))
                await asyncio.sleep(SEND_INTERVAL)
        except Exception:
            log.exception("Ошибка рассылки уведомлений по очереди %s", result.queue_id)

    def _name(self, snap: EntrySnap, users: dict[int, Row]) -> str:
        if snap.guest_name:
            return escape(snap.guest_name)
        user = users.get(snap.user_id)
        return user_name(user) if user else "Кто-то"

    def _reason(self, result: OpResult, users: dict[int, Row]) -> str:
        actor = users.get(result.actor_id) if result.actor_id else None
        if result.kind == "swap":
            a, b = result.before[result.entry_id], result.before[result.other_entry_id]
            return f"🔄 {self._name(a, users)} и {self._name(b, users)} поменялись местами."
        if result.kind in ("leave", "remove"):
            removed = result.before[result.entry_id]
            name = self._name(removed, users)
            if removed.user_id == result.actor_id and not removed.guest_name:
                return f"🚪 {name} вышел(а) из очереди — место {removed.slot} свободно."
            by = f" ({user_name(actor)})" if actor else ""
            return f"🚪 {name} убран(а) из очереди{by} — место {removed.slot} свободно."
        if result.kind in ("join", "add_classmate", "add_by_name"):
            added = result.after[result.entry_id]
            return f"➕ {self._name(added, users)} занял(а) место {added.slot}."
        if result.kind == "move":
            moved = result.after[result.entry_id]
            was = result.before[result.entry_id].slot
            return f"🔢 {self._name(moved, users)} перешёл(ла) с места {was} на место {moved.slot}."
        return "Очередь изменилась."

    def _line(self, c: Change, result: OpResult, users: dict[int, Row]) -> str:
        who = f"{escape(c.guest_name)} (ты записал(а))" if c.guest_name else "Ты"
        if c.new is None:
            return f"❌ {who}: запись убрана из очереди" if c.guest_name else "❌ Тебя убрали из очереди"
        arrow = "⬆️" if c.new < c.old else "⬇️"
        slot = result.after[c.entry_id].slot
        ahead = next((s for s in result.after.values() if s.position == c.new - 1), None)
        ahead_text = f"за <b>{self._name(ahead, users)}</b>" if ahead else "впереди никого 🎉"
        return (f"{arrow} {who}: теперь <b>{c.new}</b>-й по очереди (был {c.old}-й) · "
                f"место {slot} · {ahead_text}")

    async def _deliver(self, user_id: int, text: str, retry: bool = True) -> None:
        try:
            await self.bot.send_message(user_id, text)
        except TelegramRetryAfter as e:
            if retry:
                await asyncio.sleep(e.retry_after)
                await self._deliver(user_id, text, retry=False)
        except TelegramForbiddenError:
            # пользователь заблокировал бота — больше не пытаемся писать
            async with self.db.transaction() as conn:
                await repo.set_can_dm(conn, user_id, False)
