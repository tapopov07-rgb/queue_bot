"""Личка: /start, мои очереди, запись одногруппника."""
import logging
from html import escape
from itertools import groupby
from zoneinfo import ZoneInfo

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from config import Config
from db import repo
from db.database import Database
from handlers import admin
from keyboards import MY_QUEUES_TEXT, AddCb, MyCb, main_menu
from services.queue_service import MAX_ADDED_OTHERS, QueueError, QueueService
from services.renderer import QueueMessenger, fmt_class_time, fmt_moment, to_dt, user_name

router = Router(name="private")
router.message.filter(F.chat.type == "private")

log = logging.getLogger(__name__)

MAX_MEMBER_BUTTONS = 90  # лимит Telegram — 100 кнопок в клавиатуре

HELP_TEXT = (
    "Я веду очереди на сдачу лаб.\n\n"
    "<b>Студентам</b>\n"
    "• Записывайся кнопкой «✅ Записаться» под очередью в группе\n"
    "• «👥 Записать одногруппника» — записать ещё одного человека из потока\n"
    "• /myqueues — твои очереди и места\n\n"
    "<b>Ответственным</b>\n"
    "• /newqueue — создать очередь\n"
    "• /cancel — отменить ввод"
)


class AddForm(StatesGroup):
    name = State()


@router.message(CommandStart())
async def cmd_start(message: Message, command: CommandObject, state: FSMContext, db: Database,
                    config: Config) -> None:
    async with db.transaction() as conn:
        await repo.set_can_dm(conn, message.from_user.id, True)
    args = command.args or ""

    if args.startswith("add_") and args[4:].isdigit():
        await start_add(message, state, db, int(args[4:]))
    elif args.startswith("newq_") and args[5:].isdigit():
        await admin.begin(message, state, db, message.from_user.id, int(args[5:]))
    elif args == "my":
        await send_my_queues(message, db, config.tz)
    else:
        await state.clear()
        await message.answer(
            f"Привет, {escape(message.from_user.first_name)}! 👋\n\n{HELP_TEXT}",
            reply_markup=main_menu(),
        )


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(HELP_TEXT, reply_markup=main_menu())


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Отменено.", reply_markup=main_menu())


# ---------- запись одногруппника ----------

async def start_add(message: Message, state: FSMContext, db: Database, queue_id: int) -> None:
    user_id = message.from_user.id
    async with db.transaction() as conn:
        queue = await repo.get_queue(conn, queue_id)
        if queue is not None:
            await repo.touch_stream_member(conn, queue["stream_id"], user_id)
            added = await repo.count_added_others(conn, queue_id, user_id)
            members = await repo.get_addable_members(conn, queue_id, user_id)
    if queue is None or queue["status"] != "open":
        await message.answer("Запись в эту очередь сейчас закрыта.", reply_markup=main_menu())
        return
    if added >= MAX_ADDED_OTHERS:
        await message.answer("Ты уже записал одного человека в эту очередь — больше нельзя.\n"
                             "Убрать запись можно в /myqueues.")
        return

    await state.set_state(AddForm.name)
    await state.update_data(queue_id=queue_id)

    kb = InlineKeyboardBuilder()
    for m in members[:MAX_MEMBER_BUTTONS]:
        name = m["first_name"] + (f" {m['last_name']}" if m["last_name"] else "")
        kb.button(text=name, callback_data=AddCb(queue_id=queue_id, user_id=m["id"]))
    kb.adjust(2)
    kb.row(InlineKeyboardButton(text="❌ Отмена", callback_data=AddCb(queue_id=queue_id, user_id=0).pack()))

    text = f"👥 Кого записать в очередь <b>{escape(queue['subject'])}</b>?\n\n"
    if members:
        text += "Выбери из списка. Если человека нет в списке — напиши его имя и фамилию."
    else:
        text += ("Я пока не знаю никого из потока: список пополняется, когда люди нажимают кнопки "
                 "в группе. Напиши имя и фамилию текстом.")
    await message.answer(text, reply_markup=kb.as_markup())


async def notify_added(bot: Bot, db: Database, classmate_id: int, actor_id: int, queue_id: int,
                       position: int) -> None:
    async with db.read() as conn:
        classmate = await repo.get_user(conn, classmate_id)
        actor = await repo.get_user(conn, actor_id)
        queue = await repo.get_queue(conn, queue_id)
    if not classmate["can_dm"]:
        return
    try:
        await bot.send_message(
            classmate_id,
            f"👥 {user_name(actor)} записал(а) тебя в очередь <b>{escape(queue['subject'])}</b> — "
            f"место {position}.\nЕсли не нужно — выйди в /myqueues.",
        )
    except TelegramForbiddenError:
        async with db.transaction() as conn:
            await repo.set_can_dm(conn, classmate_id, False)


@router.callback_query(AddCb.filter())
async def cb_add(callback: CallbackQuery, callback_data: AddCb, state: FSMContext, bot: Bot, db: Database,
                 service: QueueService, messenger: QueueMessenger) -> None:
    if callback_data.user_id == 0:
        await state.clear()
        await callback.answer()
        await callback.message.edit_text("Отменено.")
        return
    try:
        res = await service.add_classmate(callback_data.queue_id, callback.from_user.id, callback_data.user_id)
    except QueueError as e:
        await callback.answer(str(e), show_alert=True)
        return
    await state.clear()
    await callback.answer()
    messenger.refresh(res.queue_id)
    async with db.read() as conn:
        classmate = await repo.get_user(conn, callback_data.user_id)
    await callback.message.edit_text(f"✅ {user_name(classmate)} записан(а) — место {res.position}.")
    await notify_added(bot, db, callback_data.user_id, callback.from_user.id, res.queue_id, res.position)


@router.message(AddForm.name, F.text)
async def on_add_name(message: Message, state: FSMContext, service: QueueService,
                      messenger: QueueMessenger) -> None:
    queue_id = (await state.get_data())["queue_id"]
    try:
        res = await service.add_by_name(queue_id, message.from_user.id, message.text)
    except QueueError as e:
        await message.answer(f"⚠️ {e}")
        if "символов" not in str(e):
            await state.clear()
        return
    await state.clear()
    messenger.refresh(queue_id)
    await message.answer(f"✅ {escape(' '.join(message.text.split()))} записан(а) — место {res.position}.",
                         reply_markup=main_menu())


# ---------- мои очереди ----------

def _full(first: str, last: str | None) -> str:
    return escape(first + (f" {last}" if last else ""))


async def build_my_queues(db: Database, user_id: int, tz: ZoneInfo) -> tuple[str, InlineKeyboardMarkup]:
    async with db.read() as conn:
        rows = await repo.get_user_active_entries(conn, user_id)

    kb = InlineKeyboardBuilder()
    if not rows:
        kb.button(text="🔄 Обновить", callback_data=MyCb(action="refresh"))
        return ("У тебя нет активных очередей.\n"
                "Записывайся кнопкой «✅ Записаться» под очередью в группе."), kb.as_markup()

    lines = ["📋 <b>Твои очереди</b>"]
    for _, group in groupby(rows, key=lambda r: r["queue_id"]):
        entries = list(group)
        q = entries[0]
        lines += ["", f"<b>{escape(q['subject'])}</b> · {fmt_class_time(q, tz)}"]
        if q["status"] == "scheduled":
            lines.append(f"   ⏳ запись откроется {fmt_moment(q['opens_at'], tz)}")
        short = f"{q['subject'][:20]} {to_dt(q['starts_at'], tz):%d.%m}"
        for e in entries:
            place = f"<b>{e['position']}</b> из {q['total']}"
            cb = MyCb(action="leave", entry_id=e["entry_id"])
            if e["user_id"] == user_id and not e["guest_name"]:
                by = "" if e["added_by"] == user_id else \
                    f" <i>(записал(а) {_full(e['adder_first_name'], e['adder_last_name'])})</i>"
                lines.append(f"   Ты: {place}{by}")
                kb.button(text=f"🚪 Выйти · {short}", callback_data=cb)
            else:
                name = escape(e["guest_name"]) if e["guest_name"] else _full(e["first_name"], e["last_name"])
                lines.append(f"   {name} <i>(ты записал(а))</i>: {place}")
                plain = e["guest_name"] or e["first_name"]
                kb.button(text=f"🚪 Убрать {plain[:15]} · {short}", callback_data=cb)
    kb.button(text="🔄 Обновить", callback_data=MyCb(action="refresh"))
    kb.adjust(1)
    return "\n".join(lines), kb.as_markup()


async def send_my_queues(message: Message, db: Database, tz: ZoneInfo) -> None:
    text, markup = await build_my_queues(db, message.from_user.id, tz)
    await message.answer(text, reply_markup=markup)


@router.message(Command("myqueues"))
@router.message(F.text == MY_QUEUES_TEXT)
async def cmd_myqueues(message: Message, db: Database, config: Config) -> None:
    await send_my_queues(message, db, config.tz)


async def _edit_my_queues(callback: CallbackQuery, db: Database, tz: ZoneInfo) -> None:
    text, markup = await build_my_queues(db, callback.from_user.id, tz)
    try:
        await callback.message.edit_text(text, reply_markup=markup)
    except TelegramBadRequest as e:
        if "not modified" not in e.message:
            raise


@router.callback_query(MyCb.filter(F.action == "refresh"))
async def cb_refresh(callback: CallbackQuery, db: Database, config: Config) -> None:
    await _edit_my_queues(callback, db, config.tz)
    await callback.answer("Обновлено")


@router.callback_query(MyCb.filter(F.action == "leave"))
async def cb_leave(callback: CallbackQuery, callback_data: MyCb, db: Database, config: Config,
                   service: QueueService, messenger: QueueMessenger) -> None:
    try:
        res = await service.leave(callback_data.entry_id, callback.from_user.id)
    except QueueError as e:
        await callback.answer(str(e), show_alert=True)
    else:
        messenger.refresh(res.queue_id)
        await callback.answer("Готово")
    await _edit_my_queues(callback, db, config.tz)
