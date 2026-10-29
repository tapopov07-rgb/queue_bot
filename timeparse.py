"""Разбор дат и времени, которые вводит ответственный."""
import re
from datetime import date, timedelta


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
