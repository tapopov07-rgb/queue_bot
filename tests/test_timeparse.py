from datetime import date

import pytest

from timeparse import ParseError, parse_date

TODAY = date(2026, 10, 14)


def test_parse_date():
    assert parse_date("завтра", TODAY) == date(2026, 10, 15)
    assert parse_date("15.10", TODAY) == date(2026, 10, 15)
    assert parse_date("10.02", TODAY) == date(2027, 2, 10)  # прошедшая дата без года → следующий год
    assert parse_date("01.12.26", TODAY) == date(2026, 12, 1)
    for bad in ("32.10", "abc", "13.10.2026"):
        with pytest.raises(ParseError):
            parse_date(bad, TODAY)
