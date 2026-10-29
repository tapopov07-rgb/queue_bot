import os
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")


@dataclass(frozen=True)
class Config:
    bot_token: str
    tz: ZoneInfo
    db_path: Path


def load_config() -> Config:
    token = os.getenv("BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("BOT_TOKEN не задан: открой файл .env и вставь токен от @BotFather")
    return Config(
        bot_token=token,
        tz=ZoneInfo(os.getenv("TZ", "Europe/Moscow")),
        db_path=BASE_DIR / os.getenv("DB_PATH", "bot.db"),
    )
