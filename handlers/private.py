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
from keyboards import MY_QUEUES_TEXT, AddCb, MyCb, SlotCb, SwapCb, main_menu
from services.notifier import Notifier
from services.queue_service import MAX_ADDED_OTHERS, QueueError, QueueService
from services.renderer import QueueMessenger, describe_place, fmt_class_day, fmt_moment, to_dt, user_name

router = Router(name="private")
router.message.filter(F.chat.type == "private")

log = logging.getLogger(__name__)

MAX_MEMBER_BUTTONS = 90  # лимит Telegram — 100 кнопок в клавиатуре

HELP_TEXT = (
    "Я веду очереди на сдачу лаб.\n\n"
    "<b>Студентам</b>\n"
    "• «✅ Записаться» под очередью в группе — первое свободное место\n"
    "• «🔢 Выбрать место» — занять конкретный номер\n"
    "• «👥 Записать одногруппника» — записать ещё одного человека из потока\n"
    "• /myqueues — твои очереди и места\n\n"
    "<b>Ответственным</b>\n"
    "• /newqueue — создать очередь\n"
    "• /deletequeue — удалить очередь\n"
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
    elif args.startswith("pick_") and args[5:].isdigit():
        text, markup = await build_slot_picker(db, config.tz, int(args[5:]), message.from_user.id, None)
        await message.answer(text, reply_markup=markup)
    elif args.startswith("newq_") and args[5:].isdigit():
        await admin.begin(message, state, db, message.from_user.id, int(args[5:]))
    elif args == "my":
        await send_my_queues(message, db, config.tz)
    elif args == "delq":
        await admin.send_delete_menu(message, db, config, message.from_user.id)
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
                       slot: int) -> None:
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
            f"место {slot}.\nЕсли не нужно — выйди в /myqueues.",
        )
    except TelegramForbiddenError:
        async with db.transaction() as conn:
            await repo.set_can_dm(conn, classmate_id, False)


@router.callback_query(AddCb.filter())
async def cb_add(callback: CallbackQuery, callback_data: AddCb, state: FSMContext, bot: Bot, db: Database,
                 service: QueueService, messenger: QueueMessenger, notifier: Notifier) -> None:
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
    notifier.notify(res)  # если заняли место впереди кого-то — тем придёт уведомление
    async with db.read() as conn:
        classmate = await repo.get_user(conn, callback_data.user_id)
    await callback.message.edit_text(f"✅ {user_name(classmate)} записан(а) — {await describe_place(db, res)}.")
    await notify_added(bot, db, callback_data.user_id, callback.from_user.id, res.queue_id, res.slot)


@router.message(AddForm.name, F.text)
async def on_add_name(message: Message, state: FSMContext, db: Database, service: QueueService,
                      messenger: QueueMessenger, notifier: Notifier) -> None:
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
    notifier.notify(res)
    await message.answer(f"✅ {escape(' '.join(message.text.split()))} записан(а) — {await describe_place(db, res)}.",
                         reply_markup=main_menu())


# ---------- выбор места ----------

async def build_slot_picker(db: Database, tz: ZoneInfo, queue_id: int, user_id: int,
                            entry_id: int | None) -> tuple[str, InlineKeyboardMarkup | None]:
    """Сетка свободных мест. entry_id=None — своя запись (если есть), иначе новая запись."""
    async with db.read() as conn:
        queue = await repo.get_queue(conn, queue_id)
        entries = await repo.get_entries(conn, queue_id) if queue else []
    if queue is None or queue["status"] != "open":
        return "Запись в эту очередь сейчас закрыта.", None

    if entry_id is None:
        mine = next((e for e in entries if e["user_id"] == user_id and not e["guest_name"]), None)
    else:
        mine = next((e for e in entries if e["id"] == entry_id and e["user_id"] == user_id), None)
        if mine is None:
            return "Этой записи уже нет в очереди.", None

    occupied = {e["slot"] for e in entries}
    free = [s for s in range(1, queue["capacity"] + 1) if s not in occupied]
    lines = [f"🔢 <b>{escape(queue['subject'])}</b> · {fmt_class_day(queue, tz)}",
             f"Свободно {len(free)} из {queue['capacity']}."]
    if mine:
        who = escape(mine["guest_name"]) if mine["guest_name"] else "Ты"
        lines.append(f"{who} сейчас на месте <b>{mine['slot']}</b> — выбери новое:")
    else:
        lines.append("Выбери место:")

    entry = mine["id"] if mine else 0
    kb = InlineKeyboardBuilder()
    if free:
        kb.row(InlineKeyboardButton(text=f"⚡ Первое свободное ({free[0]})",
                                    callback_data=SlotCb(queue_id=queue_id, entry_id=entry, slot=0).pack()))
        grid = [InlineKeyboardButton(text=str(s), callback_data=SlotCb(queue_id=queue_id, entry_id=entry,
                                                                        slot=s).pack()) for s in free]
        for i in range(0, len(grid), 8):
            kb.row(*grid[i:i + 8])
    else:
        lines.append("\n<i>Свободных мест нет.</i>")
    kb.row(InlineKeyboardButton(text="« Мои очереди", callback_data=MyCb(action="refresh").pack()))
    return "\n".join(lines), kb.as_markup()


@router.callback_query(MyCb.filter(F.action == "slot"))
async def cb_my_slot(callback: CallbackQuery, callback_data: MyCb, db: Database, config: Config) -> None:
    async with db.read() as conn:
        entry = await repo.get_entry(conn, callback_data.entry_id)
    if entry is None:
        await callback.answer("Этой записи уже нет в очереди", show_alert=True)
        return
    text, markup = await build_slot_picker(db, config.tz, entry["queue_id"], callback.from_user.id, entry["id"])
    await callback.answer()
    await callback.message.edit_text(text, reply_markup=markup)


@router.callback_query(SlotCb.filter())
async def cb_pick_slot(callback: CallbackQuery, callback_data: SlotCb, db: Database, config: Config,
                       service: QueueService, messenger: QueueMessenger, notifier: Notifier) -> None:
    slot = callback_data.slot or None
    try:
        if callback_data.entry_id:
            res = await service.move(callback_data.entry_id, slot, callback.from_user.id)
        else:
            res = await service.join(callback_data.queue_id, callback.from_user.id, slot)
    except QueueError as e:
        await callback.answer(str(e), show_alert=True)
        # место могли занять, пока смотрели на сетку, — показываем актуальную
        text, markup = await build_slot_picker(db, config.tz, callback_data.queue_id, callback.from_user.id,
                                               callback_data.entry_id or None)
        try:
            await callback.message.edit_text(text, reply_markup=markup)
        except TelegramBadRequest:
            pass
        return
    messenger.refresh(res.queue_id)
    notifier.notify(res)
    await callback.answer("Готово")
    back = InlineKeyboardBuilder().button(text="« Мои очереди", callback_data=MyCb(action="refresh"))
    await callback.message.edit_text(f"✅ Готово: {await describe_place(db, res)}.", reply_markup=back.as_markup())


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
        lines += ["", f"<b>{escape(q['subject'])}</b> · {fmt_class_day(q, tz)}"]
        if q["status"] == "scheduled":
            lines.append(f"   ⏳ запись откроется {fmt_moment(q['opens_at'], tz)}")
        short = f"{q['subject'][:18]} {to_dt(q['starts_at'], tz):%d.%m}"
        for e in entries:
            if e["prev_guest_name"]:
                ahead = f"за <b>{escape(e['prev_guest_name'])}</b>"
            elif e["prev_first_name"]:
                ahead = f"за <b>{_full(e['prev_first_name'], e['prev_last_name'])}</b>"
            else:
                ahead = "впереди никого"
            place = f"место <b>{e['slot']}</b> · {e['rank']}-й из {q['total']} · {ahead}"
            leave_cb = MyCb(action="leave", entry_id=e["entry_id"]).pack()
            if e["user_id"] == user_id and not e["guest_name"]:
                by = "" if e["added_by"] == user_id else \
                    f" <i>(записал(а) {_full(e['adder_first_name'], e['adder_last_name'])})</i>"
                lines.append(f"   Ты: {place}{by}")
                leave_text = f"🚪 Выйти · {short}"
            else:
                name = escape(e["guest_name"]) if e["guest_name"] else _full(e["first_name"], e["last_name"])
                lines.append(f"   {name} <i>(ты записал(а))</i>: {place}")
                plain = e["guest_name"] or e["first_name"]
                leave_text = f"🚪 Убрать {plain[:12]} · {short}"
            kb.row(InlineKeyboardButton(text=leave_text, callback_data=leave_cb))
            # менять место и меняться может только владелец записи (свою или вписанного по имени)
            if e["user_id"] == user_id and q["status"] == "open":
                row = [InlineKeyboardButton(text="🔢 Сменить место",
                                            callback_data=MyCb(action="slot", entry_id=e["entry_id"]).pack())]
                if q["total"] > 1:
                    row.append(InlineKeyboardButton(
                        text="🔄 Поменяться", callback_data=SwapCb(action="start", a=e["entry_id"]).pack()))
                kb.row(*row)
    kb.row(InlineKeyboardButton(text="🔄 Обновить", callback_data=MyCb(action="refresh").pack()))
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
                   service: QueueService, messenger: QueueMessenger, notifier: Notifier) -> None:
    try:
        res = await service.leave(callback_data.entry_id, callback.from_user.id)
    except QueueError as e:
        await callback.answer(str(e), show_alert=True)
    else:
        messenger.refresh(res.queue_id)
        notifier.notify(res)
        await callback.answer("Готово")
    await _edit_my_queues(callback, db, config.tz)
