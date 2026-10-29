"""Обмен местами: выбор, запрос, подтверждение второй стороной в личке."""
import logging
from html import escape

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import CallbackQuery, InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiosqlite import Row

from config import Config
from db import repo
from db.database import Database
from keyboards import MyCb, SwapCb
from services.notifier import Notifier
from services.queue_service import QueueError, QueueService
from services.renderer import QueueMessenger, fmt_class_time, user_name

router = Router(name="swap")
log = logging.getLogger(__name__)

BACK_BUTTON = InlineKeyboardButton(text="« Мои очереди", callback_data=MyCb(action="refresh").pack())


def _entry_label(e: Row) -> str:
    """Имя записи с точки зрения постороннего."""
    if e["guest_name"]:
        return f"{escape(e['guest_name'])} (записал(а) {user_name(e)})"
    return user_name(e)


def _own_label(e: Row) -> str:
    """Имя записи с точки зрения её владельца."""
    return f"Место {escape(e['guest_name'])} (ты записал(а))" if e["guest_name"] else "Твоё место"


async def _safe_edit(callback: CallbackQuery, text: str, markup=None) -> None:
    try:
        await callback.message.edit_text(text, reply_markup=markup)
    except TelegramBadRequest as e:
        if "not modified" not in e.message:
            raise


@router.callback_query(SwapCb.filter(F.action == "start"))
async def cb_start(callback: CallbackQuery, callback_data: SwapCb, db: Database, config: Config) -> None:
    me = callback.from_user.id
    async with db.read() as conn:
        mine = await repo.get_entry(conn, callback_data.a)
        queue = await repo.get_queue(conn, mine["queue_id"]) if mine else None
        entries = await repo.get_entries(conn, mine["queue_id"]) if mine else []
    if mine is None or mine["user_id"] != me or queue["status"] != "open":
        await callback.answer("Эта запись уже неактуальна", show_alert=True)
        return

    kb = InlineKeyboardBuilder()
    for e in entries:
        if e["user_id"] == me:
            continue
        mark = "" if e["can_dm"] else " 🚫"
        name = e["guest_name"] or e["first_name"] + (f" {e['last_name']}" if e["last_name"] else "")
        kb.button(text=f"{e['position']}. {name}{mark}"[:60],
                  callback_data=SwapCb(action="pick", a=mine["id"], b=e["id"]))
    kb.adjust(1)
    kb.row(BACK_BUTTON)

    who = f"{escape(mine['guest_name'])} сейчас" if mine["guest_name"] else "Ты сейчас"
    await callback.answer()
    await _safe_edit(
        callback,
        f"🔄 <b>{escape(queue['subject'])}</b> · {fmt_class_time(queue, config.tz)}\n"
        f"{who} на <b>{mine['position']}</b>-м месте. С кем поменяться?\n\n"
        "<i>🚫 — у человека не открыта личка с ботом, он не сможет подтвердить обмен.</i>",
        kb.as_markup(),
    )


@router.callback_query(SwapCb.filter(F.action == "pick"))
async def cb_pick(callback: CallbackQuery, callback_data: SwapCb, bot: Bot, db: Database,
                  config: Config, service: QueueService) -> None:
    me = callback.from_user.id
    try:
        request_id = await service.request_swap(callback_data.a, callback_data.b, me)
    except QueueError as e:
        await callback.answer(str(e), show_alert=True)
        return

    async with db.read() as conn:
        src = await repo.get_entry(conn, callback_data.a)
        dst = await repo.get_entry(conn, callback_data.b)
        queue = await repo.get_queue(conn, src["queue_id"])

    requester = (f"{user_name(src)} (за {escape(src['guest_name'])}, {src['position']}-е место)"
                 if src["guest_name"] else f"{user_name(src)} ({src['position']}-е место)")
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Принять", callback_data=SwapCb(action="accept", a=request_id))
    kb.button(text="❌ Отклонить", callback_data=SwapCb(action="decline", a=request_id))
    try:
        await bot.send_message(
            dst["user_id"],
            f"🔄 <b>Запрос на обмен</b>\n📋 <b>{escape(queue['subject'])}</b> · {fmt_class_time(queue, config.tz)}\n\n"
            f"{requester} предлагает поменяться местами.\n"
            f"{_own_label(dst)}: <b>{dst['position']}</b> → <b>{src['position']}</b>\n\n"
            "<i>Запрос действует 30 минут.</i>",
            reply_markup=kb.as_markup(),
        )
    except TelegramForbiddenError:
        async with db.transaction() as conn:
            await repo.set_can_dm(conn, dst["user_id"], False)
        await service.cancel_swap(request_id, me)
        await callback.answer("Не получилось отправить запрос: человек заблокировал бота", show_alert=True)
        return

    await callback.answer("Запрос отправлен")
    cancel = InlineKeyboardButton(text="❌ Отменить запрос",
                                  callback_data=SwapCb(action="cancel", a=request_id).pack())
    await _safe_edit(
        callback,
        f"⏳ Запрос на обмен отправлен: <b>{_entry_label(dst)}</b> ({dst['position']}-е место).\n"
        "Ждём ответа — до 30 минут. Я напишу, когда он ответит.",
        InlineKeyboardBuilder().row(cancel).row(BACK_BUTTON).as_markup(),
    )


@router.callback_query(SwapCb.filter(F.action == "accept"))
async def cb_accept(callback: CallbackQuery, callback_data: SwapCb, service: QueueService,
                    messenger: QueueMessenger, notifier: Notifier) -> None:
    try:
        res = await service.accept_swap(callback_data.a, callback.from_user.id)
    except QueueError as e:
        await callback.answer(str(e), show_alert=True)
        await callback.message.edit_reply_markup(reply_markup=None)
        return
    messenger.refresh(res.queue_id)
    notifier.notify(res)  # инициатор обмена получит «ты теперь N-й»
    new_pos = res.after[res.other_entry_id].position
    await callback.answer("Готово")
    await _safe_edit(callback, callback.message.html_text + f"\n\n✅ <b>Обмен выполнен</b> — теперь {new_pos}-е место.")


@router.callback_query(SwapCb.filter(F.action == "decline"))
async def cb_decline(callback: CallbackQuery, callback_data: SwapCb, bot: Bot, db: Database,
                     service: QueueService) -> None:
    try:
        req = await service.decline_swap(callback_data.a, callback.from_user.id)
    except QueueError as e:
        await callback.answer(str(e), show_alert=True)
        await callback.message.edit_reply_markup(reply_markup=None)
        return
    await callback.answer("Отклонено")
    await _safe_edit(callback, callback.message.html_text + "\n\n❌ Ты отклонил(а) обмен.")

    async with db.read() as conn:
        src = await repo.get_entry(conn, req["from_entry_id"])
        queue = await repo.get_queue(conn, req["queue_id"])
        me = await repo.get_user(conn, callback.from_user.id)
    if src is not None and src["can_dm"]:
        try:
            await bot.send_message(
                src["user_id"],
                f"❌ {user_name(me)} отклонил(а) обмен в очереди <b>{escape(queue['subject'])}</b>.",
            )
        except TelegramForbiddenError:
            pass


@router.callback_query(SwapCb.filter(F.action == "cancel"))
async def cb_cancel(callback: CallbackQuery, callback_data: SwapCb, service: QueueService) -> None:
    try:
        await service.cancel_swap(callback_data.a, callback.from_user.id)
    except QueueError as e:
        await callback.answer(str(e), show_alert=True)
        return
    await callback.answer("Запрос отменён")
    await _safe_edit(callback, "Запрос на обмен отменён.", InlineKeyboardBuilder().row(BACK_BUTTON).as_markup())
