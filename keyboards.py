"""Клавиатуры и callback data."""
from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

MY_QUEUES_TEXT = "📋 Мои очереди"


class QueueCb(CallbackData, prefix="q"):
    action: str  # join | leave | wait
    queue_id: int


class MyCb(CallbackData, prefix="my"):
    action: str  # leave | refresh
    entry_id: int = 0


class SwapCb(CallbackData, prefix="sw"):
    action: str  # start | pick | accept | decline | cancel
    a: int = 0   # start: моя запись; pick: моя запись; accept/decline/cancel: id запроса
    b: int = 0   # pick: запись, с которой меняемся


class AddCb(CallbackData, prefix="add"):
    queue_id: int
    user_id: int  # 0 — отмена


class NewQueueCb(CallbackData, prefix="nq"):
    action: str  # stream | preset | confirm | cancel
    value: str = ""


def deep_link(bot_username: str, payload: str) -> str:
    return f"https://t.me/{bot_username}?start={payload}"


def main_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text=MY_QUEUES_TEXT)]], resize_keyboard=True)


def presets(*pairs: tuple[str, str]) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for text, value in pairs:
        kb.button(text=text, callback_data=NewQueueCb(action="preset", value=value))
    kb.button(text="❌ Отмена", callback_data=NewQueueCb(action="cancel"))
    kb.adjust(len(pairs), 1)
    return kb.as_markup()


def cancel_only() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data=NewQueueCb(action="cancel"))
    return kb.as_markup()
