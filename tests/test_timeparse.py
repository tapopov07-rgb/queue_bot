from datetime import date, datetime, time
from zoneinfo import ZoneInfo

import pytest

from timeparse import ParseError, parse_date, parse_duration, parse_opens, parse_time

TZ = ZoneInfo("Europe/Moscow")
TODAY = date(2026, 10, 14)


def test_parse_date():
    assert parse_date("завтра", TODAY) == date(2026, 10, 15)
    assert parse_date("15.10", TODAY) == date(2026, 10, 15)
    assert parse_date("10.02", TODAY) == date(2027, 2, 10)  # прошедшая дата без года → следующий год
    assert parse_date("01.12.26", TODAY) == date(2026, 12, 1)
    for bad in ("32.10", "abc", "13.10.2026"):
        with pytest.raises(ParseError):
            parse_date(bad, TODAY)


def test_parse_time_and_duration():
    assert parse_time("9:05") == time(9, 5)
    assert parse_time("10.00") == time(10, 0)
    assert parse_duration("90") == 90
    assert parse_duration("45 мин") == 45
    for bad in ("25:00", "9"):
        with pytest.raises(ParseError):
            parse_time(bad)
    with pytest.raises(ParseError):
        parse_duration("5")


def test_parse_opens():
    starts = datetime(2026, 10, 15, 10, 0, tzinfo=TZ)
    now = datetime(2026, 10, 14, 12, 0, tzinfo=TZ)
    assert parse_opens("сейчас", starts, now) == now
    assert parse_opens("за 24 ч", starts, now) == datetime(2026, 10, 14, 10, 0, tzinfo=TZ)
    assert parse_opens("24h", starts, now) == datetime(2026, 10, 14, 10, 0, tzinfo=TZ)
    assert parse_opens("20:00", starts, now) == datetime(2026, 10, 14, 20, 0, tzinfo=TZ)  # накануне
    assert parse_opens("08:00", starts, now) == datetime(2026, 10, 15, 8, 0, tzinfo=TZ)   # в день пары
    assert parse_opens("14.10 18:30", starts, now) == datetime(2026, 10, 14, 18, 30, tzinfo=TZ)
    with pytest.raises(ParseError):
        parse_opens("когда-нибудь", starts, now)
