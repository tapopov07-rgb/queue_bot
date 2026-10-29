import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import ErrorEvent

from config import load_config
from db.database import Database
from handlers import admin, group, private
from middlewares import UserMiddleware
from services.queue_service import QueueService
from services.renderer import QueueMessenger
from services.scheduler import QueueScheduler


async def on_error(event: ErrorEvent) -> bool | None:
    # Нажатие кнопки, сделанное пока бот был выключен: действие выполнено,
    # но ответить на нажатие Telegram уже не даёт. Это не ошибка.
    if isinstance(event.exception, TelegramBadRequest) and "query is too old" in event.exception.message:
        logging.info("Пропущен ответ на старое нажатие кнопки")
        return True
    return None  # остальные ошибки aiogram залогирует сам, с подробностями


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("apscheduler").setLevel(logging.WARNING)
    config = load_config()

    db = Database(config.db_path)
    await db.init()

    bot = Bot(config.bot_token, default=DefaultBotProperties(
        parse_mode=ParseMode.HTML,
        allow_sending_without_reply=True,  # исходное сообщение удалили — ответим без цитаты
    ))
    me = await bot.get_me()

    service = QueueService(db)
    messenger = QueueMessenger(bot, db, config.tz, me.username)
    scheduler = QueueScheduler(db, service, messenger, config.tz)

    dp = Dispatcher(
        config=config, db=db, service=service, messenger=messenger,
        scheduler=scheduler, bot_username=me.username,
    )
    dp.update.outer_middleware(UserMiddleware())
    dp.errors.register(on_error)
    dp.include_routers(private.router, admin.router, group.router)

    await scheduler.start()
    logging.info("Бот @%s запущен", me.username)
    try:
        await dp.start_polling(bot)
    finally:
        scheduler.shutdown()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
