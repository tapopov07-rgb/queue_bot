import asyncio
import logging
from datetime import datetime
from logging.handlers import RotatingFileHandler
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import ErrorEvent

from config import BASE_DIR, load_config
from db.database import Database
from handlers import admin, group, private, schedule, swap
from middlewares import UserMiddleware
from services.notifier import Notifier
from services.planner import SchedulePlanner
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


def setup_logging(tz: ZoneInfo) -> None:
    """Логи в консоль и в logs/bot.log (5 файлов по 1 МБ, старые удаляются)."""
    log_dir = BASE_DIR / "logs"
    log_dir.mkdir(exist_ok=True)
    file_handler = RotatingFileHandler(log_dir / "bot.log", maxBytes=1_000_000, backupCount=5, encoding="utf-8")
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    # время в логах — в часовом поясе из настроек, а не в системном
    # (на Windows переменная TZ=Europe/Moscow непонятна системе, и логи шли бы по UTC)
    formatter.converter = lambda ts: datetime.fromtimestamp(ts, tz).timetuple()
    handlers = [logging.StreamHandler(), file_handler]
    for handler in handlers:
        handler.setFormatter(formatter)
    logging.basicConfig(level=logging.INFO, handlers=handlers)
    logging.getLogger("apscheduler").setLevel(logging.WARNING)


async def main() -> None:
    config = load_config()
    setup_logging(config.tz)

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
    notifier = Notifier(bot, db, config.tz)
    planner = SchedulePlanner(bot, db, service, messenger, scheduler, config.tz)

    dp = Dispatcher(
        config=config, db=db, service=service, messenger=messenger, scheduler=scheduler,
        notifier=notifier, planner=planner, bot_username=me.username,
    )
    dp.update.outer_middleware(UserMiddleware())
    dp.errors.register(on_error)
    dp.include_routers(private.router, admin.router, schedule.router, swap.router, group.router)

    await scheduler.start()
    planner.setup()
    created = await planner.generate()  # догнать полночь, если бот был выключен
    if created:
        logging.info("При запуске созданы очереди по расписанию: %s", created)
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
