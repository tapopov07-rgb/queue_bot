"""Ответственный: расписание лабораторных (диалог в личке)."""
import re
from datetime import date, datetime
from html import escape

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from config import Config
from db import repo
from db.database import Database
from keyboards import SchedCb
from services.planner import SchedulePlanner, fmt_day, interval_text, upcoming_dates
from services.queue_service import DEFAULT_CAPACITY, MAX_CAPACITY, MAX_SUBJECT_LEN, QueueError
from timeparse import ParseError, parse_date

router = Router(name="schedule")
router.message.filter(F.chat.type == "private")


class SchedForm(StatesGroup):
    subject = State()
    first_date = State()
    interval = State()
    until = State()
    capacity = State()
    confirm = State()


def _today(config: Config) -> date:
    return datetime.now(config.tz).date()


def _kb(*rows: list[tuple[str, SchedCb]]) -> InlineKeyboardMarkup:
    """Клавиатура из рядов [(текст, callback), ...]."""
    kb = InlineKeyboardBuilder()
    for row in rows:
        kb.row(*[InlineKeyboardButton(text=text, callback_data=cb.pack()) for text, cb in row])
    return kb.as_markup()


def _presets(*pairs: tuple[str, str]) -> InlineKeyboardMarkup:
    return _kb([(text, SchedCb(action="preset", value=value)) for text, value in pairs],
               [("❌ Отмена", SchedCb(action="cancel"))])


async def _show(target: Message | CallbackQuery, text: str, markup: InlineKeyboardMarkup | None) -> None:
    """Ответить новым сообщением или отредактировать текущее (если пришли по кнопке)."""
    if isinstance(target, CallbackQuery):
        try:
            await target.message.edit_text(text, reply_markup=markup)
        except TelegramBadRequest as e:
            if "not modified" not in e.message:
                raise
    else:
        await target.answer(text, reply_markup=markup)


# ---------- список ----------

async def render_list(db: Database, config: Config, stream_id: int) -> tuple[str, InlineKeyboardMarkup]:
    async with db.read() as conn:
        stream = await repo.get_stream(conn, stream_id)
        items = await repo.get_stream_schedule(conn, stream_id)
        skips = {s["id"]: await repo.get_schedule_skips(conn, s["id"]) for s in items}

    lines = [f"📅 <b>Расписание · {escape(stream['title'])}</b>", ""]
    rows = []
    if not items:
        lines.append("<i>Пока пусто. Добавь лабораторную — очереди будут открываться сами "
                     "в 00:00 накануне пары.</i>")
    for s in items:
        until = f" до {date.fromisoformat(s['until_date']):%d.%m}" if s["until_date"] else ""
        nxt = upcoming_dates(s, _today(config), skips[s["id"]], 1)
        lines.append(f"• <b>{escape(s['subject'])}</b> — {interval_text(s['interval_weeks'])}{until}, мест {s['capacity']}")
        lines.append(f"   ближайшая: {fmt_day(nxt[0])}" if nxt else "   <i>больше дат нет</i>")
        rows.append([(f"⚙️ {s['subject'][:40]}", SchedCb(action="item", sid=s["id"]))])
    rows.append([("➕ Добавить лабораторную", SchedCb(action="add", sid=stream_id))])
    return "\n".join(lines), _kb(*rows)


async def send_schedule_menu(target: Message | CallbackQuery, db: Database, config: Config, user_id: int) -> None:
    async with db.read() as conn:
        streams = await repo.get_admin_streams(conn, user_id)
    if not streams:
        await _show(target, "Расписание ведёт ответственный потока. Ты пока не ответственный ни в одном.", None)
        return
    if len(streams) == 1:
        text, markup = await render_list(db, config, streams[0]["id"])
        await _show(target, text, markup)
        return
    await _show(target, "Расписание какого потока открыть?",
                _kb(*[[(s["title"], SchedCb(action="list", sid=s["id"]))] for s in streams]))


@router.message(Command("schedule"))
async def cmd_schedule(message: Message, state: FSMContext, db: Database, config: Config) -> None:
    await state.clear()
    await send_schedule_menu(message, db, config, message.from_user.id)


@router.callback_query(SchedCb.filter(F.action == "list"))
async def cb_list(callback: CallbackQuery, callback_data: SchedCb, state: FSMContext, db: Database,
                  config: Config) -> None:
    await state.clear()
    async with db.read() as conn:
        allowed = await repo.is_stream_admin(conn, callback_data.sid, callback.from_user.id)
    if not allowed:
        await callback.answer("Это не твой поток", show_alert=True)
        return
    text, markup = await render_list(db, config, callback_data.sid)
    await callback.answer()
    await _show(callback, text, markup)


# ---------- одна пара ----------

async def render_item(db: Database, config: Config, schedule_id: int) -> tuple[str, InlineKeyboardMarkup] | None:
    async with db.read() as conn:
        s = await repo.get_schedule(conn, schedule_id)
        if s is None:
            return None
        skips = await repo.get_schedule_skips(conn, schedule_id)
    dates = upcoming_dates(s, _today(config), skips, 4)
    until = f"{date.fromisoformat(s['until_date']):%d.%m.%Y}" if s["until_date"] else "без конца"
    lines = [
        f"⚙️ <b>{escape(s['subject'])}</b>",
        f"Повтор: {interval_text(s['interval_weeks'])}, с {date.fromisoformat(s['first_date']):%d.%m}",
        f"До: {until} · мест: {s['capacity']}",
        "",
        "Ближайшие даты: " + (" · ".join(fmt_day(d) for d in dates) if dates else "<i>нет</i>"),
    ]
    rows = []
    if dates:
        rows.append([(f"⏭ Пропустить {fmt_day(dates[0])}",
                      SchedCb(action="skip", sid=schedule_id, value=dates[0].isoformat()))])
    rows.append([("🗑 Удалить из расписания", SchedCb(action="delete", sid=schedule_id))])
    rows.append([("« Назад", SchedCb(action="list", sid=s["stream_id"]))])
    return "\n".join(lines), _kb(*rows)


@router.callback_query(SchedCb.filter(F.action == "item"))
async def cb_item(callback: CallbackQuery, callback_data: SchedCb, db: Database, config: Config) -> None:
    rendered = await render_item(db, config, callback_data.sid)
    if rendered is None:
        await callback.answer("Этой пары уже нет в расписании", show_alert=True)
        return
    await callback.answer()
    await _show(callback, *rendered)


@router.callback_query(SchedCb.filter(F.action.in_({"skip", "skipdate"})))
async def cb_skip(callback: CallbackQuery, callback_data: SchedCb, db: Database, config: Config,
                  planner: SchedulePlanner) -> None:
    class_date = date.fromisoformat(callback_data.value)
    try:
        exists = await planner.skip(callback_data.sid, class_date, callback.from_user.id)
    except QueueError as e:
        await callback.answer(str(e), show_alert=True)
        return
    if exists:
        await callback.answer(f"Пропущено. Очередь на {fmt_day(class_date)} уже открыта — "
                              "удалить её можно через /deletequeue", show_alert=True)
    else:
        await callback.answer(f"Пропущено: {fmt_day(class_date)}")
    if callback_data.action == "skipdate":  # кнопка из напоминания
        await _show(callback, callback.message.html_text + f"\n\n⏭ <b>Пропущено</b> — очередь на "
                                                          f"{fmt_day(class_date)} не откроется.", None)
    else:
        rendered = await render_item(db, config, callback_data.sid)
        if rendered:
            await _show(callback, *rendered)


@router.callback_query(SchedCb.filter(F.action == "delete"))
async def cb_delete(callback: CallbackQuery, callback_data: SchedCb, db: Database) -> None:
    async with db.read() as conn:
        s = await repo.get_schedule(conn, callback_data.sid)
    if s is None:
        await callback.answer("Этой пары уже нет в расписании", show_alert=True)
        return
    await callback.answer()
    await _show(callback,
                f"Удалить <b>{escape(s['subject'])}</b> из расписания?\n"
                "Новые очереди перестанут создаваться. Уже открытые очереди останутся.",
                _kb([("🗑 Да, удалить", SchedCb(action="delconfirm", sid=s["id"]))],
                    [("« Назад", SchedCb(action="item", sid=s["id"]))]))


@router.callback_query(SchedCb.filter(F.action == "delconfirm"))
async def cb_delete_confirm(callback: CallbackQuery, callback_data: SchedCb, db: Database, config: Config,
                            planner: SchedulePlanner) -> None:
    try:
        s = await planner.delete(callback_data.sid, callback.from_user.id)
    except QueueError as e:
        await callback.answer(str(e), show_alert=True)
        return
    await callback.answer("Удалено")
    text, markup = await render_list(db, config, s["stream_id"])
    await _show(callback, text, markup)


# ---------- добавление ----------

@router.callback_query(SchedCb.filter(F.action == "add"))
async def cb_add(callback: CallbackQuery, callback_data: SchedCb, state: FSMContext, db: Database) -> None:
    async with db.read() as conn:
        allowed = await repo.is_stream_admin(conn, callback_data.sid, callback.from_user.id)
    if not allowed:
        await callback.answer("Это не твой поток", show_alert=True)
        return
    await state.clear()
    await state.set_state(SchedForm.subject)
    await state.update_data(stream_id=callback_data.sid)
    await callback.answer()
    await callback.message.answer(
        "➕ Новая лабораторная в расписании.\n\nНапиши название — предмет и что сдаём.\n"
        "Например: <i>Матан — лабы</i>",
        reply_markup=_kb([("❌ Отмена", SchedCb(action="cancel"))]),
    )


@router.callback_query(SchedCb.filter(F.action == "cancel"))
async def cb_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.answer("Отменено")
    await callback.message.edit_text("❌ Отменено.")


async def step_subject(text: str, reply: Message, state: FSMContext, config: Config) -> None:
    subject = " ".join(text.split())
    if not subject or len(subject) > MAX_SUBJECT_LEN:
        raise ParseError(f"Название — от 1 до {MAX_SUBJECT_LEN} символов")
    await state.update_data(subject=subject)
    await state.set_state(SchedForm.first_date)
    await reply.answer("📅 Когда <b>первая</b> лабораторная? Например <b>04.11</b>.",
                       reply_markup=_presets(("Сегодня", "today"), ("Завтра", "tomorrow")))


async def step_first_date(text: str, reply: Message, state: FSMContext, config: Config) -> None:
    d = parse_date(text, _today(config))
    await state.update_data(first_date=d.isoformat())
    await state.set_state(SchedForm.interval)
    await reply.answer(f"🔁 Как часто, начиная с {fmt_day(d)}?",
                       reply_markup=_presets(("Каждую неделю", "1"), ("Раз в 2 недели", "2")))


async def step_interval(text: str, reply: Message, state: FSMContext, config: Config) -> None:
    if text.strip() not in ("1", "2"):
        raise ParseError("Выбери кнопкой: каждую неделю или раз в 2 недели")
    await state.update_data(interval=int(text))
    await state.set_state(SchedForm.until)
    await reply.answer("🏁 До какого числа повторять? Например <b>25.12</b> — последняя возможная дата.",
                       reply_markup=_presets(("Без конца", "none")))


async def step_until(text: str, reply: Message, state: FSMContext, config: Config) -> None:
    data = await state.get_data()
    first = date.fromisoformat(data["first_date"])
    until = None
    if text.strip().lower() not in ("none", "без конца"):
        until = parse_date(text, _today(config))
        if until < first:
            raise ParseError(f"Дата окончания раньше первой лабораторной ({fmt_day(first)})")
    await state.update_data(until=until.isoformat() if until else None)
    await state.set_state(SchedForm.capacity)
    await reply.answer(f"🔢 Сколько мест в каждой очереди? Обычно {DEFAULT_CAPACITY}.",
                       reply_markup=_presets((f"{DEFAULT_CAPACITY} мест", str(DEFAULT_CAPACITY))))


async def step_capacity(text: str, reply: Message, state: FSMContext, config: Config) -> None:
    m = re.fullmatch(r"(\d{1,3})\s*(?:мест\w*)?", text.strip().lower())
    if not m or not 1 <= int(m[1]) <= MAX_CAPACITY:
        raise ParseError(f"Напиши число от 1 до {MAX_CAPACITY}")
    await state.update_data(capacity=int(m[1]))
    await state.set_state(SchedForm.confirm)
    data = await state.get_data()
    preview = {"first_date": data["first_date"], "interval_weeks": data["interval"], "until_date": data["until"]}
    dates = upcoming_dates(preview, _today(config), set(), 4)
    until = f"{date.fromisoformat(data['until']):%d.%m}" if data["until"] else "без конца"
    await reply.answer(
        "Проверь:\n\n"
        f"📋 <b>{escape(data['subject'])}</b>\n"
        f"🔁 {interval_text(data['interval'])}, до: {until}\n"
        f"🔢 Мест: {data['capacity']}\n"
        f"📅 Даты: {' · '.join(fmt_day(d) for d in dates)}{'…' if len(dates) == 4 else ''}\n\n"
        "<i>Запись на каждую дату откроется в 00:00 накануне и закроется в 23:59 дня пары. "
        "Накануне открытия в 18:00 я напомню — можно будет пропустить дату.</i>",
        reply_markup=_kb([("✅ Сохранить", SchedCb(action="save")), ("❌ Отмена", SchedCb(action="cancel"))]),
    )


STEPS = {
    SchedForm.subject.state: step_subject,
    SchedForm.first_date.state: step_first_date,
    SchedForm.interval.state: step_interval,
    SchedForm.until.state: step_until,
    SchedForm.capacity.state: step_capacity,
}


async def _run_step(text: str, reply: Message, state: FSMContext, config: Config) -> None:
    step = STEPS.get(await state.get_state())
    if step is None:
        return
    try:
        await step(text, reply, state, config)
    except ParseError as e:
        await reply.answer(f"⚠️ {e}")


@router.message(SchedForm.subject, F.text)
@router.message(SchedForm.first_date, F.text)
@router.message(SchedForm.interval, F.text)
@router.message(SchedForm.until, F.text)
@router.message(SchedForm.capacity, F.text)
async def on_step_text(message: Message, state: FSMContext, config: Config) -> None:
    await _run_step(message.text, message, state, config)


@router.callback_query(SchedCb.filter(F.action == "preset"))
async def cb_preset(callback: CallbackQuery, callback_data: SchedCb, state: FSMContext, config: Config) -> None:
    await callback.answer()
    await callback.message.edit_reply_markup(reply_markup=None)
    await _run_step(callback_data.value, callback.message, state, config)


@router.callback_query(SchedForm.confirm, SchedCb.filter(F.action == "save"))
async def cb_save(callback: CallbackQuery, state: FSMContext, db: Database, config: Config,
                  planner: SchedulePlanner) -> None:
    data = await state.get_data()
    try:
        await planner.add(
            stream_id=data["stream_id"], subject=data["subject"], first_date=date.fromisoformat(data["first_date"]),
            interval_weeks=data["interval"], until_date=date.fromisoformat(data["until"]) if data["until"] else None,
            capacity=data["capacity"], actor_id=callback.from_user.id,
        )
    except QueueError as e:
        await callback.answer(str(e), show_alert=True)
        return
    await state.clear()
    await callback.answer("Сохранено")
    created = await planner.generate()  # если первая пара сегодня или завтра — очередь откроется сразу
    note = "\n\n🟢 Запись на ближайшую дату уже открыта — очередь опубликована в группе." if created else ""
    await callback.message.edit_text(f"✅ <b>{escape(data['subject'])}</b> добавлена в расписание.{note}")
    text, markup = await render_list(db, config, data["stream_id"])
    await callback.message.answer(text, reply_markup=markup)
