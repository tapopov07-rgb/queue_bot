"""Разбор дат и времени, которые вводит ответственный."""
import re
from datetime import date, datetime, time, timedelta


class ParseError(ValueError):
    pass


def parse_date(text: str, today: date) -> date:
    t = text.strip().lower()
    if t in ("сегодня", "today"):
        return today
    if t in ("завтра", "tomorrow"):
        return today + timedelta(days=1)
    if t == "послезавтра":
        return today + timedelta(days=2)

    m = re.fullmatch(r"(\d{1,2})[./](\d{1,2})(?:[./](\d{4}|\d{2}))?", t)
    if not m:
        raise ParseError("Не понял дату. Напиши, например, <b>15.10</b> или «завтра»")
    day, month, year_s = int(m[1]), int(m[2]), m[3]
    if year_s is None:
        year = today.year
    else:
        year = int(year_s) + (2000 if len(year_s) == 2 else 0)
    try:
        result = date(year, month, day)
        if year_s is None and result < today:
            result = date(year + 1, month, day)
    except ValueError:
        raise ParseError("Такой даты нет") from None
    if result < today:
        raise ParseError("Эта дата уже прошла")
    return result


def parse_time(text: str) -> time:
    m = re.fullmatch(r"(\d{1,2})[:.](\d{2})", text.strip())
    if not m or int(m[1]) > 23 or int(m[2]) > 59:
        raise ParseError("Не понял время. Напиши, например, <b>10:00</b>")
    return time(int(m[1]), int(m[2]))


def parse_duration(text: str) -> int:
    m = re.fullmatch(r"(\d{1,3})\s*(?:мин\.?|m|min)?", text.strip().lower())
    if not m or not 10 <= int(m[1]) <= 600:
        raise ParseError("Напиши длительность в минутах, от 10 до 600, например <b>90</b>")
    return int(m[1])


def parse_opens(text: str, starts_at: datetime, now: datetime) -> datetime:
    """Когда открыть запись:
    «сейчас» · «за 24 ч» · «ДД.ММ ЧЧ:ММ» · «ЧЧ:ММ» (ближайшее такое время до начала пары)."""
    t = text.strip().lower()
    if t in ("сейчас", "now"):
        return now

    m = re.fullmatch(r"(?:за\s*)?(\d{1,3})\s*(?:ч|час|часа|часов|h)\.?", t)
    if m:
        return starts_at - timedelta(hours=int(m[1]))

    m = re.fullmatch(r"(\S+)\s+(\d{1,2}[:.]\d{2})", t)
    if m:
        d = parse_date(m[1], now.date())
        return datetime.combine(d, parse_time(m[2]), tzinfo=starts_at.tzinfo)

    if re.fullmatch(r"\d{1,2}[:.]\d{2}", t):
        tm = parse_time(t)
        candidate = starts_at.replace(hour=tm.hour, minute=tm.minute, second=0, microsecond=0)
        if candidate >= starts_at:
            candidate -= timedelta(days=1)
        return candidate

    raise ParseError(
        "Не понял. Варианты: <b>сейчас</b>, <b>за 24 ч</b>, <b>20:00</b> (накануне/в день пары) "
        "или <b>14.10 20:00</b>"
    )
