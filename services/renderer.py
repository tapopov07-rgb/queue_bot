"""Текст и клавиатура сообщения очереди + синхронное обновление во всех чатах потока."""
import asyncio
import logging
from datetime import datetime
from html import escape
from zoneinfo import ZoneInfo

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiosqlite import Row

from db import repo
from db.database import Database
from keyboards import QueueCb, deep_link

log = logging.getLogger(__name__)

WEEKDAYS = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]


def to_dt(ts: int, tz: ZoneInfo) -> datetime:
    return datetime.fromtimestamp(ts, tz)


def fmt_moment(ts: int, tz: ZoneInfo) -> str:
    d = to_dt(ts, tz)
    return f"{WEEKDAYS[d.weekday()]}, {d:%d.%m} в {d:%H:%M}"


def fmt_class_day(queue: Row, tz: ZoneInfo) -> str:
    """День пары: «ср, 30.10»."""
    d = to_dt(queue["starts_at"], tz)
    return f"{WEEKDAYS[d.weekday()]}, {d:%d.%m}"


def user_name(row: Row) -> str:
    name = row["first_name"] + (f" {row['last_name']}" if row["last_name"] else "")
    return escape(name)


def entry_name(entry: Row) -> str:
    if entry["guest_name"]:
        return f"{escape(entry['guest_name'])} <i>(записал(а) {user_name(entry)})</i>"
    return user_name(entry)


async def describe_place(db: Database, result, *, html: bool = True) -> str:
    """«место 6 · 4-й по очереди · за Иван Петров» для записи из результата операции."""
    ahead = next((s for s in result.after.values() if s.position == result.position - 1), None)
    if ahead is None:
        ahead_text = "впереди никого"
    else:
        if ahead.guest_name:
            name = ahead.guest_name
        else:
            async with db.read() as conn:
                user = await repo.get_user(conn, ahead.user_id)
            name = user["first_name"] + (f" {user['last_name']}" if user["last_name"] else "")
        ahead_text = f"за <b>{escape(name)}</b>" if html else f"за {name}"
    return f"место {result.slot} · {result.position}-й по очереди · {ahead_text}"


def render_queue(queue: Row, entries: list[Row], tz: ZoneInfo) -> str:
    """Занятые места по порядку номеров; пустые места между ними — одной строкой «свободно: N»."""
    free = queue["capacity"] - len(entries)
    status = {
        "scheduled": f"⏳ Запись откроется {fmt_moment(queue['opens_at'], tz)}",
        "open": f"🟢 Запись до {fmt_moment(queue['ends_at'], tz)} · свободно {free} из {queue['capacity']}",
        "closed": "🔒 Запись закрыта",
    }[queue["status"]]
    lines = [
        f"📋 <b>{escape(queue['subject'])}</b>",
        f"🗓 {fmt_class_day(queue, tz)}",
        status,
        "",
    ]
    prev_slot = 0
    for e in entries:  # по возрастанию номера места
        gap = e["slot"] - prev_slot - 1
        if gap > 0:
            lines.append(f"<i>   ┄ свободно: {gap} ┄</i>")
        lines.append(f"{e['slot']}. {entry_name(e)}")
        prev_slot = e["slot"]
    if not entries and queue["status"] != "scheduled":
        lines.append("<i>Пока никто не записался</i>")
    return "\n".join(lines)


def queue_keyboard(queue: Row, bot_username: str, tz: ZoneInfo) -> InlineKeyboardMarkup | None:
    kb = InlineKeyboardBuilder()
    qid = queue["id"]
    if queue["status"] == "scheduled":
        opens = to_dt(queue["opens_at"], tz)
        kb.button(text=f"⏳ Откроется {opens:%d.%m в %H:%M}", callback_data=QueueCb(action="wait", queue_id=qid))
        kb.adjust(1)
    elif queue["status"] == "open":
        kb.button(text="✅ Записаться", callback_data=QueueCb(action="join", queue_id=qid))
        kb.button(text="🚪 Выйти", callback_data=QueueCb(action="leave", queue_id=qid))
        kb.button(text="🔢 Выбрать место", url=deep_link(bot_username, f"pick_{qid}"))
        kb.button(text="👥 Записать одногруппника", url=deep_link(bot_username, f"add_{qid}"))
        kb.button(text="📋 Мои очереди", url=deep_link(bot_username, "my"))
        kb.adjust(2, 1, 1, 1)
    else:
        return None
    return kb.as_markup()


class QueueMessenger:
    """Публикует сообщение очереди во все чаты потока и держит их в актуальном виде.
    Обновления сгруппированы (debounce), чтобы не упираться в лимиты Telegram на edit."""

    DEBOUNCE_SECONDS = 1.0

    def __init__(self, bot: Bot, db: Database, tz: ZoneInfo, bot_username: str) -> None:
        self.bot = bot
        self.db = db
        self.tz = tz
        self.bot_username = bot_username
        self._pending: dict[int, asyncio.Task] = {}

    async def _render(self, queue_id: int) -> tuple[Row, str, InlineKeyboardMarkup | None] | None:
        async with self.db.read() as conn:
            queue = await repo.get_queue(conn, queue_id)
            if queue is None:
                return None
            entries = await repo.get_entries(conn, queue_id)
        return queue, render_queue(queue, entries, self.tz), queue_keyboard(queue, self.bot_username, self.tz)

    async def publish(self, queue_id: int) -> int:
        """Отправить сообщение очереди во все активные чаты потока. Возвращает число чатов."""
        rendered = await self._render(queue_id)
        if rendered is None:
            return 0
        queue, text, markup = rendered
        async with self.db.read() as conn:
            chat_ids = await repo.get_stream_active_chats(conn, queue["stream_id"])

        sent: list[tuple[int, int]] = []
        for chat_id in chat_ids:
            try:
                msg = await self.bot.send_message(chat_id, text, reply_markup=markup)
                sent.append((chat_id, msg.message_id))
            except (TelegramForbiddenError, TelegramBadRequest) as e:
                log.warning("Не удалось отправить очередь %s в чат %s: %s", queue_id, chat_id, e)
        async with self.db.transaction() as conn:
            for chat_id, message_id in sent:
                await repo.save_queue_message(conn, queue_id, chat_id, message_id)
        return len(sent)

    async def mark_deleted(self, queue: Row, messages: list[Row]) -> None:
        """Заменить сообщения удалённой очереди на «удалена» и убрать кнопки."""
        task = self._pending.pop(queue["id"], None)
        if task:
            task.cancel()
        text = f"🗑 Очередь <b>{escape(queue['subject'])}</b> ({fmt_class_day(queue, self.tz)}) удалена."
        for m in messages:
            try:
                await self.bot.edit_message_text(text=text, chat_id=m["chat_id"], message_id=m["message_id"],
                                                 reply_markup=None)
            except (TelegramBadRequest, TelegramForbiddenError) as e:
                log.warning("Не удалось пометить удалённую очередь %s в чате %s: %s", queue["id"], m["chat_id"], e)

    def refresh(self, queue_id: int) -> None:
        """Запланировать обновление сообщений очереди (можно вызывать сколько угодно часто)."""
        if queue_id in self._pending:
            return  # уже запланировано — возьмёт самое свежее состояние
        self._pending[queue_id] = asyncio.create_task(self._refresh_later(queue_id))

    async def _refresh_later(self, queue_id: int) -> None:
        try:
            await asyncio.sleep(self.DEBOUNCE_SECONDS)
            self._pending.pop(queue_id, None)
            await self._edit_all(queue_id)
        except Exception:
            log.exception("Ошибка обновления очереди %s", queue_id)
        finally:
            if self._pending.get(queue_id) is asyncio.current_task():
                self._pending.pop(queue_id, None)

    async def _edit_all(self, queue_id: int) -> None:
        rendered = await self._render(queue_id)
        if rendered is None:
            return
        _, text, markup = rendered
        async with self.db.read() as conn:
            messages = await repo.get_queue_messages(conn, queue_id)
        for m in messages:
            await self._edit_one(queue_id, m["chat_id"], m["message_id"], text, markup)

    async def _edit_one(self, queue_id: int, chat_id: int, message_id: int, text: str,
                        markup: InlineKeyboardMarkup | None, retry: bool = True) -> None:
        try:
            await self.bot.edit_message_text(text=text, chat_id=chat_id, message_id=message_id,
                                             reply_markup=markup)
        except TelegramRetryAfter as e:
            if retry:
                await asyncio.sleep(e.retry_after)
                await self._edit_one(queue_id, chat_id, message_id, text, markup, retry=False)
        except TelegramBadRequest as e:
            if "not modified" in e.message:
                return
            log.warning("Сообщение очереди %s в чате %s недоступно: %s", queue_id, chat_id, e)
            async with self.db.transaction() as conn:
                await repo.delete_queue_message(conn, queue_id, chat_id)
        except TelegramForbiddenError:
            async with self.db.transaction() as conn:
                await repo.delete_queue_message(conn, queue_id, chat_id)
