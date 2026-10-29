"""Ответственный: создание (диалог в личке) и удаление очередей."""
from datetime import date, datetime, time as dtime, timedelta
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
from services.queue_service import MAX_SUBJECT_LEN, QueueError, QueueService
from services.renderer import QueueMessenger, fmt_class_time, fmt_moment, to_dt
from services.scheduler import QueueScheduler
from timeparse import ParseError, parse_date, parse_duration, parse_opens, parse_time

router = Router(name="admin")
router.message.filter(F.chat.type == "private")


class NewQueue(StatesGroup):
    subject = State()
    date = State()
    start = State()
    duration = State()
    opens = State()
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
    await reply.answer("📅 Когда пара? Напиши дату, например <b>15.10</b>.",
                       reply_markup=presets(("Сегодня", "today"), ("Завтра", "tomorrow")))


async def step_date(text: str, reply: Message, state: FSMContext, config: Config) -> None:
    d = parse_date(text, _now(config).date())
    await state.update_data(date=d.isoformat())
    await state.set_state(NewQueue.start)
    await reply.answer(f"🕘 Во сколько начинается пара {d:%d.%m}? Например <b>10:00</b>.",
                       reply_markup=cancel_only())


async def step_start(text: str, reply: Message, state: FSMContext, config: Config) -> None:
    t = parse_time(text)
    await state.update_data(start=t.strftime("%H:%M"))
    await state.set_state(NewQueue.duration)
    await reply.answer("⏱ Сколько длится пара (в минутах)?", reply_markup=presets(("90 мин", "90")))


async def step_duration(text: str, reply: Message, state: FSMContext, config: Config) -> None:
    minutes = parse_duration(text)
    data = await state.get_data()
    starts = datetime.combine(date.fromisoformat(data["date"]), dtime.fromisoformat(data["start"]),
                              tzinfo=config.tz)
    if starts + timedelta(minutes=minutes) <= _now(config):
        await state.set_state(NewQueue.date)
        await reply.answer("Эта пара уже закончилась. Введи дату заново.",
                           reply_markup=presets(("Сегодня", "today"), ("Завтра", "tomorrow")))
        return
    await state.update_data(duration=minutes)
    await state.set_state(NewQueue.opens)
    await reply.answer(
        "🔓 Когда открыть запись?\n"
        "• <b>сейчас</b>\n• <b>за 24 ч</b> — за сколько часов до пары\n"
        "• <b>20:00</b> — ближайшие 20:00 перед парой\n• <b>14.10 20:00</b> — точная дата и время",
        reply_markup=presets(("Сейчас", "now"), ("За 24 ч", "24h"), ("За 12 ч", "12h")),
    )


async def step_opens(text: str, reply: Message, state: FSMContext, config: Config) -> None:
    data = await state.get_data()
    starts = datetime.combine(date.fromisoformat(data["date"]), dtime.fromisoformat(data["start"]),
                              tzinfo=config.tz)
    ends = starts + timedelta(minutes=data["duration"])
    now = _now(config)
    opens = parse_opens(text, starts, now)
    if opens >= ends:
        raise ParseError("Запись должна открыться до конца пары")
    opens = max(opens, now)

    await state.update_data(starts_at=int(starts.timestamp()), ends_at=int(ends.timestamp()),
                            opens_at=int(opens.timestamp()))
    await state.set_state(NewQueue.confirm)
    opens_text = "сразу" if opens == now else fmt_moment(int(opens.timestamp()), config.tz)
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Создать", callback_data=NewQueueCb(action="confirm"))
    kb.button(text="❌ Отмена", callback_data=NewQueueCb(action="cancel"))
    await reply.answer(
        "Проверь:\n\n"
        f"📋 <b>{escape(data['subject'])}</b>\n"
        f"👥 Поток: {escape(data['stream_title'])}\n"
        f"🗓 {starts:%d.%m}, {starts:%H:%M}–{ends:%H:%M}\n"
        f"🔓 Запись откроется: {opens_text}",
        reply_markup=kb.as_markup(),
    )


STEPS = {
    NewQueue.subject.state: step_subject,
    NewQueue.date.state: step_date,
    NewQueue.start.state: step_start,
    NewQueue.duration.state: step_duration,
    NewQueue.opens.state: step_opens,
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
@router.message(NewQueue.start, F.text)
@router.message(NewQueue.duration, F.text)
@router.message(NewQueue.opens, F.text)
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
            ends_at=data["ends_at"], opens_at=data["opens_at"], actor_id=callback.from_user.id,
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
        kb.button(text=f"🗑 {q['subject'][:30]} · {to_dt(q['starts_at'], config.tz):%d.%m %H:%M}",
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
        f"Удалить очередь <b>{escape(queue['subject'])}</b> ({fmt_class_time(queue, config.tz)})?\n"
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
