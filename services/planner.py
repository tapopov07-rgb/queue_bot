"""Расписание: правила повторения, автосоздание очередей, напоминания ответственным, очистка.

Пара в расписании повторяется каждую неделю или раз в 2 недели начиная с first_date.
Очередь на дату D создаётся в 00:00 накануне (D−1) — это и есть момент открытия записи.
Накануне открытия (D−2 в 18:00) ответственные получают напоминание с кнопкой «Пропустить».
"""
import logging
import sqlite3
from datetime import date, datetime, timedelta
from html import escape
from zoneinfo import ZoneInfo

from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiosqlite import Row

from db import repo
from db.database import Database
from keyboards import SchedCb
from services.permissions import Action, can
from services.queue_service import QueueError, QueueService, queue_window
from services.renderer import WEEKDAYS

log = logging.getLogger(__name__)

KEEP_CLOSED_DAYS = 7  # закрытые очереди хранятся неделю, потом удаляются
REMIND_HOUR = 18


def fmt_day(d: date) -> str:
    return f"{WEEKDAYS[d.weekday()]}, {d:%d.%m}"


def interval_text(weeks: int) -> str:
    return "каждую неделю" if weeks == 1 else "раз в 2 недели"


def occurs_on(schedule: Row, d: date) -> bool:
    first = date.fromisoformat(schedule["first_date"])
    if d < first:
        return False
    if schedule["until_date"] and d > date.fromisoformat(schedule["until_date"]):
        return False
    return (d - first).days % (7 * schedule["interval_weeks"]) == 0


def upcoming_dates(schedule: Row, from_date: date, skips: set[str], limit: int) -> list[date]:
    """Ближайшие даты пары, начиная с from_date (включительно), без пропущенных."""
    first = date.fromisoformat(schedule["first_date"])
    until = date.fromisoformat(schedule["until_date"]) if schedule["until_date"] else None
    step = 7 * schedule["interval_weeks"]
    if from_date <= first:
        d = first
    else:
        d = first + timedelta(days=-(-(from_date - first).days // step) * step)  # округление вверх
    result = []
    while len(result) < limit and (until is None or d <= until):
        if d.isoformat() not in skips:
            result.append(d)
        d += timedelta(days=step)
    return result


class SchedulePlanner:
    def __init__(self, bot: Bot, db: Database, service: QueueService, messenger, scheduler, tz: ZoneInfo) -> None:
        self.bot = bot
        self.db = db
        self.service = service
        self.messenger = messenger    # QueueMessenger: publish(queue_id)
        self.scheduler = scheduler    # QueueScheduler: plan(queue), add_daily(...)
        self.tz = tz

    def setup(self) -> None:
        self.scheduler.add_daily("schedule:generate", self.generate, hour=0, minute=0, second=5)
        self.scheduler.add_daily("schedule:remind", self.remind, hour=REMIND_HOUR)
        self.scheduler.add_daily("cleanup", self.cleanup, hour=4)

    def today(self) -> date:
        return datetime.now(self.tz).date()

    # ---------- управление расписанием ----------

    async def add(self, *, stream_id: int, subject: str, first_date: date, interval_weeks: int,
                  until_date: date | None, capacity: int, actor_id: int) -> int:
        async with self.db.transaction() as conn:
            if not await can(conn, actor_id, Action.MANAGE_SCHEDULE, stream_id=stream_id):
                raise QueueError("Расписание может менять только ответственный потока")
            schedule_id = await repo.create_schedule(
                conn, stream_id=stream_id, subject=subject, first_date=first_date.isoformat(),
                interval_weeks=interval_weeks, until_date=until_date.isoformat() if until_date else None,
                capacity=capacity, created_by=actor_id,
            )
        log.info("Пара «%s» добавлена в расписание потока %s (%s)", subject, stream_id, schedule_id)
        return schedule_id

    async def delete(self, schedule_id: int, actor_id: int) -> Row:
        async with self.db.transaction() as conn:
            schedule = await self._get_managed(conn, schedule_id, actor_id)
            await repo.delete_schedule(conn, schedule_id)
        return schedule

    async def skip(self, schedule_id: int, class_date: date, actor_id: int) -> bool:
        """Пропустить дату. Возвращает True, если очередь на неё уже создана (её надо удалять отдельно)."""
        _, starts_at, _ = queue_window(class_date, self.tz)
        async with self.db.transaction() as conn:
            await self._get_managed(conn, schedule_id, actor_id)
            await repo.add_schedule_skip(conn, schedule_id, class_date.isoformat())
            return await repo.find_schedule_queue(conn, schedule_id, starts_at) is not None

    async def _get_managed(self, conn, schedule_id: int, actor_id: int) -> Row:
        schedule = await repo.get_schedule(conn, schedule_id)
        if schedule is None:
            raise QueueError("Этой пары уже нет в расписании")
        if not await can(conn, actor_id, Action.MANAGE_SCHEDULE, stream_id=schedule["stream_id"]):
            raise QueueError("Расписание может менять только ответственный потока")
        return schedule

    # ---------- ежедневные задачи ----------

    async def generate(self) -> list[int]:
        """Создать очереди, чья запись уже открыта (пара сегодня или завтра) и ещё не создана.
        Запускается в 00:00:05 и при старте бота — так пропущенная полночь догоняется."""
        today = self.today()
        async with self.db.read() as conn:
            schedules = await repo.get_all_schedule(conn)
            skips = {s["id"]: await repo.get_schedule_skips(conn, s["id"]) for s in schedules}
        created = []
        for s in schedules:
            for d in (today, today + timedelta(days=1)):
                if not occurs_on(s, d) or d.isoformat() in skips[s["id"]]:
                    continue
                queue_id = await self._create(s, d)
                if queue_id:
                    created.append(queue_id)
        return created

    async def _create(self, schedule: Row, d: date) -> int | None:
        opens_at, starts_at, ends_at = queue_window(d, self.tz)
        async with self.db.read() as conn:
            if await repo.find_schedule_queue(conn, schedule["id"], starts_at):
                return None
        try:
            queue_id = await self.service.create_queue(
                stream_id=schedule["stream_id"], subject=schedule["subject"], starts_at=starts_at,
                ends_at=ends_at, opens_at=opens_at, capacity=schedule["capacity"], actor_id=None,
                schedule_id=schedule["id"],
            )
        except (QueueError, sqlite3.IntegrityError) as e:  # IntegrityError — уже создана параллельно
            log.warning("Не создана очередь по расписанию %s на %s: %s", schedule["id"], d, e)
            return None
        log.info("Очередь %s «%s» на %s создана по расписанию", queue_id, schedule["subject"], d)
        await self.messenger.publish(queue_id)
        async with self.db.read() as conn:
            self.scheduler.plan(await repo.get_queue(conn, queue_id))
        return queue_id

    async def remind(self) -> int:
        """Напомнить ответственным о записи, которая откроется завтра в 00:00. Возвращает число напоминаний."""
        class_date = self.today() + timedelta(days=2)
        sent = 0
        async with self.db.read() as conn:
            schedules = await repo.get_all_schedule(conn)
            for s in schedules:
                if not occurs_on(s, class_date):
                    continue
                if class_date.isoformat() in await repo.get_schedule_skips(conn, s["id"]):
                    continue
                users = await repo.get_users(conn, await repo.get_stream_admin_ids(conn, s["stream_id"]))
                kb = InlineKeyboardBuilder()
                kb.button(text="⏭ Пропустить — пары не будет",
                          callback_data=SchedCb(action="skipdate", sid=s["id"], value=class_date.isoformat()))
                for user in users.values():
                    if not user["can_dm"]:
                        continue
                    try:
                        await self.bot.send_message(
                            user["id"],
                            f"📅 Завтра в 00:00 открою запись: <b>{escape(s['subject'])}</b> "
                            f"(пара {fmt_day(class_date)}).\nЕсли лабораторной не будет — нажми «Пропустить».",
                            reply_markup=kb.as_markup(),
                        )
                        sent += 1
                    except TelegramForbiddenError:
                        log.info("Ответственный %s заблокировал бота — напоминание не доставлено", user["id"])
        return sent

    async def cleanup(self) -> int:
        cutoff = repo.now() - KEEP_CLOSED_DAYS * 24 * 3600
        async with self.db.transaction() as conn:
            removed = await repo.delete_closed_queues_before(conn, cutoff)
        if removed:
            log.info("Удалено закрытых очередей старше %d дней: %d", KEEP_CLOSED_DAYS, removed)
        return removed
