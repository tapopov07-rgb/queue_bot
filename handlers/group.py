"""Групповые чаты: регистрация чата, кнопки очереди."""
import logging

from aiogram import F, Router
from aiogram.filters import IS_MEMBER, IS_NOT_MEMBER, ChatMemberUpdatedFilter, Command
from aiogram.types import CallbackQuery, ChatMemberUpdated, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from db import repo
from db.database import Database
from keyboards import QueueCb, deep_link
from services.notifier import Notifier
from services.queue_service import QueueError, QueueService
from services.renderer import QueueMessenger, describe_place, user_name

router = Router(name="group")
GROUPS = {"group", "supergroup"}
router.my_chat_member.filter(F.chat.type.in_(GROUPS))

log = logging.getLogger(__name__)


@router.my_chat_member(ChatMemberUpdatedFilter(IS_NOT_MEMBER >> IS_MEMBER))
async def bot_added(event: ChatMemberUpdated, db: Database) -> None:
    chat, adder = event.chat, event.from_user
    async with db.transaction() as conn:
        await repo.upsert_chat(conn, chat.id, chat.title or str(chat.id), adder.id)
        streams = await repo.get_chat_streams(conn, chat.id)
        if not streams:
            stream_id = await repo.create_stream(conn, chat.title or str(chat.id))
            await repo.add_stream_chat(conn, stream_id, chat.id)
            await repo.add_stream_admin(conn, stream_id, adder.id)
        await repo.touch_chat_member(conn, chat.id, adder.id)
        admin = await repo.get_user(conn, adder.id)
    log.info("Бота добавили в чат %s (%s), добавил %s", chat.title, chat.id, adder.id)
    await event.answer(
        "Привет! Я помогу с очередями 📋\n\n"
        f"Ответственный: {user_name(admin)} — создаёт очереди командой /newqueue.\n"
        "Остальные записываются кнопками под сообщением очереди."
    )


@router.my_chat_member(ChatMemberUpdatedFilter(IS_MEMBER >> IS_NOT_MEMBER))
async def bot_removed(event: ChatMemberUpdated, db: Database) -> None:
    async with db.transaction() as conn:
        await repo.set_chat_active(conn, event.chat.id, False)
    log.info("Бота удалили из чата %s (%s)", event.chat.title, event.chat.id)


@router.message(F.migrate_to_chat_id)
async def chat_migrated(message: Message, db: Database) -> None:
    async with db.transaction() as conn:
        await repo.migrate_chat(conn, message.chat.id, message.migrate_to_chat_id)
    log.info("Чат %s стал супергруппой %s", message.chat.id, message.migrate_to_chat_id)


@router.message(Command("newqueue"), F.chat.type.in_(GROUPS))
async def group_newqueue(message: Message, db: Database, bot_username: str) -> None:
    async with db.read() as conn:
        streams = [s for s in await repo.get_chat_streams(conn, message.chat.id)
                   if await repo.is_stream_admin(conn, s["id"], message.from_user.id)]
    if not streams:
        await message.reply("Создавать очереди может только ответственный.")
        return
    kb = InlineKeyboardBuilder()
    kb.button(text="🆕 Создать очередь", url=deep_link(bot_username, f"newq_{streams[0]['id']}"))
    await message.reply("Очередь создаётся в личке со мной:", reply_markup=kb.as_markup())


@router.message(Command("schedule"), F.chat.type.in_(GROUPS))
async def group_schedule(message: Message, bot_username: str) -> None:
    kb = InlineKeyboardBuilder()
    kb.button(text="📅 Открыть расписание", url=deep_link(bot_username, "sched"))
    await message.reply("Расписание ведётся в личке со мной (только ответственный):", reply_markup=kb.as_markup())


@router.message(Command("deletequeue"), F.chat.type.in_(GROUPS))
async def group_deletequeue(message: Message, bot_username: str) -> None:
    kb = InlineKeyboardBuilder()
    kb.button(text="🗑 Удалить очередь", url=deep_link(bot_username, "delq"))
    await message.reply("Очереди удаляются в личке со мной (только ответственный):", reply_markup=kb.as_markup())


@router.callback_query(QueueCb.filter(F.action == "join"))
async def cb_join(callback: CallbackQuery, callback_data: QueueCb, db: Database, service: QueueService,
                  messenger: QueueMessenger, notifier: Notifier, bot_username: str) -> None:
    try:
        res = await service.join(callback_data.queue_id, callback.from_user.id)  # первое свободное место
    except QueueError as e:
        await callback.answer(str(e), show_alert=True)
        return
    messenger.refresh(res.queue_id)
    notifier.notify(res)  # первое свободное может оказаться впереди кого-то
    async with db.read() as conn:
        user = await repo.get_user(conn, callback.from_user.id)
    # всплывающее окно — простой текст, без HTML
    text = f"✅ Ты записан: {await describe_place(db, res, html=False)}"
    if not user["can_dm"]:
        text += f"\n\nЧтобы получать уведомления о сдвиге места, открой @{bot_username} и нажми «Start»"
    await callback.answer(text, show_alert=not user["can_dm"])


@router.callback_query(QueueCb.filter(F.action == "leave"))
async def cb_leave(callback: CallbackQuery, callback_data: QueueCb, service: QueueService,
                   messenger: QueueMessenger, notifier: Notifier) -> None:
    try:
        res = await service.leave_queue(callback_data.queue_id, callback.from_user.id)
    except QueueError as e:
        await callback.answer(str(e), show_alert=True)
        return
    messenger.refresh(res.queue_id)
    notifier.notify(res)
    await callback.answer("🚪 Запись удалена")


@router.callback_query(QueueCb.filter(F.action == "wait"))
async def cb_wait(callback: CallbackQuery) -> None:
    await callback.answer("Запись ещё не открыта — кнопка появится в указанное время")
