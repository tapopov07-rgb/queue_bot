"""Ответственный: создание (диалог в личке) и удаление очередей."""
import re
from datetime import date, datetime
from html import escape

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from config import Config
from db import repo
from db.database import Database
from keyboards import DelCb, NewQueueCb, cancel_only, presets
from services.queue_service import (DEFAULT_CAPACITY, MAX_CAPACITY, MAX_SUBJECT_LEN, QueueError,
                                    QueueService, queue_window)
from services.renderer import QueueMessenger, fmt_class_day, fmt_moment, to_dt
from services.scheduler import QueueScheduler
from timeparse import ParseError, parse_date

router = Router(name="admin")
router.message.filter(F.chat.type == "private")


class NewQueue(StatesGroup):
    subject = State()
    date = State()
    capacity = State()
    confirm = State()


async def begin(message: Message, state: FSMContext, db: Database, user_id: int, stream_id: int) -> None:
    async with db.read() as conn:
        stream = await repo.get_stream(conn, stream_id)
        allowed = stream is not None and await repo.is_stream_admin(conn, stream_id, user_id)
    if not allowed:
        await message.answer("Создавать очереди в этом потоке может только ответственный.")
        return
    await state.clear()
    await state.set_state(NewQueue.subject)
    await state.update_data(stream_id=stream_id, stream_title=stream["title"])
    await message.answer(
        f"🆕 Новая очередь для <b>{escape(stream['title'])}</b>\n\n"
        "Напиши название — предмет и что сдаём.\nНапример: <i>Матан — лаба 3</i>",
        reply_markup=cancel_only(),
    )


@router.message(Command("newqueue"))
async def cmd_newqueue(message: Message, state: FSMContext, db: Database) -> None:
    async with db.read() as conn:
        streams = await repo.get_admin_streams(conn, message.from_user.id)
    if not streams:
        await message.answer(
            "Ты пока не ответственный ни в одном потоке.\n"
            "Добавь меня в группу — тот, кто добавил, становится ответственным."
        )
        return
    if len(streams) == 1:
        await begin(message, state, db, message.from_user.id, streams[0]["id"])
        return
    kb = InlineKeyboardBuilder()
    for s in streams:
        kb.button(text=s["title"], callback_data=NewQueueCb(action="stream", value=str(s["id"])))
    kb.adjust(1)
    await message.answer("Для какого потока создать очередь?", reply_markup=kb.as_markup())


@router.callback_query(NewQueueCb.filter(F.action == "stream"))
async def cb_stream(callback: CallbackQuery, callback_data: NewQueueCb, state: FSMContext, db: Database) -> None:
    await callback.answer()
    await callback.message.edit_reply_markup(reply_markup=None)
    await begin(callback.message, state, db, callback.from_user.id, int(callback_data.value))


@router.callback_query(NewQueueCb.filter(F.action == "cancel"))
async def cb_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.answer("Отменено")
    await callback.message.edit_text("❌ Создание очереди отменено.")


# ---------- шаги диалога: (текст, ответить_в, state, config) ----------

def _now(config: Config) -> datetime:
    return datetime.now(config.tz)


async def step_subject(text: str, reply: Message, state: FSMContext, config: Config) -> None:
    subject = " ".join(text.split())
    if not subject or len(subject) > MAX_SUBJECT_LEN:
        await reply.answer(f"Название — от 1 до {MAX_SUBJECT_LEN} символов. Попробуй ещё раз.")
        return
    await state.update_data(subject=subject)
    await state.set_state(NewQueue.date)
    await reply.answer("📅 Когда пара? Напиши дату, например <b>15.10</b>.\n"
                       "<i>Запись откроется в 00:00 накануне и закроется в 23:59 дня пары.</i>",
                       reply_markup=presets(("Сегодня", "today"), ("Завтра", "tomorrow")))


async def step_date(text: str, reply: Message, state: FSMContext, config: Config) -> None:
    d = parse_date(text, _now(config).date())
    await state.update_data(date=d.isoformat())
    await state.set_state(NewQueue.capacity)
    await reply.answer(f"🔢 Сколько мест в очереди? Обычно {DEFAULT_CAPACITY}.",
                       reply_markup=presets((f"{DEFAULT_CAPACITY} мест", str(DEFAULT_CAPACITY))))


async def step_capacity(text: str, reply: Message, state: FSMContext, config: Config) -> None:
    m = re.fullmatch(r"(\d{1,3})\s*(?:мест\w*)?", text.strip().lower())
    if not m or not 1 <= int(m[1]) <= MAX_CAPACITY:
        raise ParseError(f"Напиши число от 1 до {MAX_CAPACITY}, например <b>{DEFAULT_CAPACITY}</b>")
    capacity = int(m[1])
    data = await state.get_data()
    opens_at, starts_at, ends_at = queue_window(date.fromisoformat(data["date"]), config.tz)
    opens_text = "сразу" if opens_at <= _now(config).timestamp() else fmt_moment(opens_at, config.tz)
    await state.update_data(capacity=capacity, opens_at=opens_at, starts_at=starts_at, ends_at=ends_at)
    await state.set_state(NewQueue.confirm)

    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Создать", callback_data=NewQueueCb(action="confirm"))
    kb.button(text="❌ Отмена", callback_data=NewQueueCb(action="cancel"))
    await reply.answer(
        "Проверь:\n\n"
        f"📋 <b>{escape(data['subject'])}</b>\n"
        f"👥 Поток: {escape(data['stream_title'])}\n"
        f"🗓 Пара: {fmt_class_day({'starts_at': starts_at}, config.tz)}\n"
        f"🔢 Мест: {capacity}\n"
        f"🔓 Запись: {opens_text} — до {fmt_moment(ends_at, config.tz)}",
        reply_markup=kb.as_markup(),
    )


STEPS = {
    NewQueue.subject.state: step_subject,
    NewQueue.date.state: step_date,
    NewQueue.capacity.state: step_capacity,
}


async def _run_step(text: str, reply: Message, state: FSMContext, config: Config) -> None:
    step = STEPS.get(await state.get_state())
    if step is None:
        return
    try:
        await step(text, reply, state, config)
    except ParseError as e:
        await reply.answer(f"⚠️ {e}")


@router.message(NewQueue.subject, F.text)
@router.message(NewQueue.date, F.text)
@router.message(NewQueue.capacity, F.text)
async def on_step_text(message: Message, state: FSMContext, config: Config) -> None:
    await _run_step(message.text, message, state, config)


@router.callback_query(NewQueueCb.filter(F.action == "preset"))
async def cb_preset(callback: CallbackQuery, callback_data: NewQueueCb, state: FSMContext, config: Config) -> None:
    await callback.answer()
    await callback.message.edit_reply_markup(reply_markup=None)
    await _run_step(callback_data.value, callback.message, state, config)


@router.callback_query(NewQueue.confirm, NewQueueCb.filter(F.action == "confirm"))
async def cb_confirm(callback: CallbackQuery, state: FSMContext, db: Database, service: QueueService,
                     messenger: QueueMessenger, scheduler: QueueScheduler) -> None:
    data = await state.get_data()
    try:
        queue_id = await service.create_queue(
            stream_id=data["stream_id"], subject=data["subject"], starts_at=data["starts_at"],
            ends_at=data["ends_at"], opens_at=data["opens_at"], capacity=data["capacity"],
            actor_id=callback.from_user.id,
        )
    except QueueError as e:
        await callback.answer(str(e), show_alert=True)
        return
    await state.clear()
    await callback.answer()

    sent = await messenger.publish(queue_id)
    async with db.read() as conn:
        scheduler.plan(await repo.get_queue(conn, queue_id))

    if sent:
        await callback.message.edit_text(f"✅ Очередь <b>{escape(data['subject'])}</b> опубликована в чатах: {sent}.")
    else:
        await callback.message.edit_text(
            "⚠️ Очередь создана, но я не смог отправить её ни в один чат потока. "
            "Проверь, что я всё ещё состою в группе."
        )


# ---------- удаление очереди ----------

async def send_delete_menu(message: Message, db: Database, config: Config, user_id: int) -> None:
    async with db.read() as conn:
        queues = await repo.get_admin_active_queues(conn, user_id)
    if not queues:
        await message.answer("Нет активных очередей, которые ты можешь удалить.")
        return
    kb = InlineKeyboardBuilder()
    for q in queues:
        kb.button(text=f"🗑 {q['subject'][:30]} · {to_dt(q['starts_at'], config.tz):%d.%m}",
                  callback_data=DelCb(action="pick", queue_id=q["id"]))
    kb.button(text="❌ Отмена", callback_data=DelCb(action="cancel"))
    kb.adjust(1)
    await message.answer("Какую очередь удалить?", reply_markup=kb.as_markup())


@router.message(Command("deletequeue"))
async def cmd_deletequeue(message: Message, db: Database, config: Config) -> None:
    await send_delete_menu(message, db, config, message.from_user.id)


@router.callback_query(DelCb.filter(F.action == "pick"))
async def cb_delete_pick(callback: CallbackQuery, callback_data: DelCb, db: Database, config: Config) -> None:
    async with db.read() as conn:
        queue = await repo.get_queue(conn, callback_data.queue_id)
        total = len(await repo.get_entries(conn, callback_data.queue_id)) if queue else 0
    if queue is None:
        await callback.answer("Очередь уже удалена", show_alert=True)
        return
    kb = InlineKeyboardBuilder()
    kb.button(text="🗑 Да, удалить", callback_data=DelCb(action="confirm", queue_id=queue["id"]))
    kb.button(text="❌ Отмена", callback_data=DelCb(action="cancel"))
    await callback.answer()
    await callback.message.edit_text(
        f"Удалить очередь <b>{escape(queue['subject'])}</b> ({fmt_class_day(queue, config.tz)})?\n"
        f"Записано: {total}. Это действие нельзя отменить.",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(DelCb.filter(F.action == "confirm"))
async def cb_delete_confirm(callback: CallbackQuery, callback_data: DelCb, service: QueueService,
                            messenger: QueueMessenger, scheduler: QueueScheduler) -> None:
    try:
        queue, messages = await service.delete_queue(callback_data.queue_id, callback.from_user.id)
    except QueueError as e:
        await callback.answer(str(e), show_alert=True)
        return
    scheduler.unplan(queue["id"])
    await callback.answer("Удалено")
    await callback.message.edit_text(f"🗑 Очередь <b>{escape(queue['subject'])}</b> удалена.")
    await messenger.mark_deleted(queue, messages)


@router.callback_query(DelCb.filter(F.action == "cancel"))
async def cb_delete_cancel(callback: CallbackQuery) -> None:
    await callback.answer()
    await callback.message.edit_text("Отменено.")
