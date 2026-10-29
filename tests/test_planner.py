from datetime import date, timedelta
from zoneinfo import ZoneInfo

import pytest

from db import repo
from services.planner import SchedulePlanner, occurs_on, upcoming_dates
from services.queue_service import QueueError, queue_window
from tests.conftest import ADMIN

TZ = ZoneInfo("Europe/Moscow")


def sched(first, weeks=1, until=None):
    return {"first_date": first, "interval_weeks": weeks, "until_date": until}


def test_occurs_on_weekly_and_biweekly():
    weekly = sched("2026-11-04")
    biweekly = sched("2026-11-04", weeks=2, until="2026-12-02")
    assert occurs_on(weekly, date(2026, 11, 11))
    assert not occurs_on(weekly, date(2026, 11, 12))
    assert not occurs_on(weekly, date(2026, 10, 28))           # до первой пары
    assert occurs_on(biweekly, date(2026, 11, 18))
    assert not occurs_on(biweekly, date(2026, 11, 11))         # неделя без лабы
    assert occurs_on(biweekly, date(2026, 12, 2))              # последняя включительно
    assert not occurs_on(biweekly, date(2026, 12, 16))         # после окончания


def test_upcoming_dates():
    s = sched("2026-11-04", weeks=2)
    assert upcoming_dates(s, date(2026, 10, 1), set(), 2) == [date(2026, 11, 4), date(2026, 11, 18)]
    assert upcoming_dates(s, date(2026, 11, 5), set(), 1) == [date(2026, 11, 18)]
    assert upcoming_dates(s, date(2026, 11, 18), {"2026-11-18"}, 1) == [date(2026, 12, 2)]  # пропуск
    assert upcoming_dates(sched("2026-11-04", until="2026-11-04"), date(2026, 11, 5), set(), 3) == []


class FakeMessenger:
    def __init__(self):
        self.published = []

    async def publish(self, queue_id):
        self.published.append(queue_id)
        return 1


class FakeScheduler:
    def __init__(self):
        self.planned = []

    def plan(self, queue):
        self.planned.append(queue["id"])


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append((chat_id, text))


@pytest.fixture
async def planner_env(env, monkeypatch):
    db, service, stream_id, _ = env
    planner = SchedulePlanner(FakeBot(), db, service, FakeMessenger(), FakeScheduler(), TZ)
    today = date(2026, 11, 3)  # вторник
    monkeypatch.setattr(planner, "today", lambda: today)
    # «сейчас» для проверки, что день ещё не прошёл, — полдень вторника
    monkeypatch.setattr(repo, "now", lambda: queue_window(today, TZ)[1] + 12 * 3600)
    return db, planner, stream_id, today


async def test_generate_creates_tomorrow_once(planner_env):
    db, planner, stream_id, today = planner_env
    wed = today + timedelta(days=1)
    sid = await planner.add(stream_id=stream_id, subject="Матан — лаба", first_date=wed, interval_weeks=2,
                            until_date=None, capacity=25, actor_id=ADMIN)
    created = await planner.generate()
    assert len(created) == 1
    assert await planner.generate() == []  # повторный запуск не создаёт дубль
    async with db.read() as conn:
        q = await repo.get_queue(conn, created[0])
    assert (q["schedule_id"], q["capacity"], q["status"]) == (sid, 25, "open")
    assert (q["opens_at"], q["starts_at"], q["ends_at"]) == queue_window(wed, TZ)
    assert planner.messenger.published == created and planner.scheduler.planned == created


async def test_generate_respects_skip_and_interval(planner_env):
    _, planner, stream_id, today = planner_env
    wed = today + timedelta(days=1)
    sid = await planner.add(stream_id=stream_id, subject="X", first_date=wed - timedelta(days=7), interval_weeks=2,
                            until_date=None, capacity=30, actor_id=ADMIN)
    assert await planner.generate() == []  # неделя без лабы
    sid2 = await planner.add(stream_id=stream_id, subject="Y", first_date=wed, interval_weeks=1,
                             until_date=None, capacity=30, actor_id=ADMIN)
    assert await planner.skip(sid2, wed, ADMIN) is False
    assert await planner.generate() == []  # пропущено
    assert sid != sid2


async def test_generate_catches_up_today(planner_env):
    _, planner, stream_id, today = planner_env
    await planner.add(stream_id=stream_id, subject="Сегодня", first_date=today, interval_weeks=1,
                      until_date=None, capacity=30, actor_id=ADMIN)
    assert len(await planner.generate()) == 1  # бот проспал полночь — запись на сегодня всё равно открыта


async def test_skip_after_creation_reports_existing_queue(planner_env):
    _, planner, stream_id, today = planner_env
    wed = today + timedelta(days=1)
    sid = await planner.add(stream_id=stream_id, subject="X", first_date=wed, interval_weeks=1,
                            until_date=None, capacity=30, actor_id=ADMIN)
    await planner.generate()
    assert await planner.skip(sid, wed, ADMIN) is True


async def test_only_admin_manages(planner_env):
    _, planner, stream_id, today = planner_env
    with pytest.raises(QueueError):
        await planner.add(stream_id=stream_id, subject="X", first_date=today, interval_weeks=1,
                          until_date=None, capacity=30, actor_id=5)
    sid = await planner.add(stream_id=stream_id, subject="X", first_date=today, interval_weeks=1,
                            until_date=None, capacity=30, actor_id=ADMIN)
    with pytest.raises(QueueError):
        await planner.skip(sid, today, 5)
    with pytest.raises(QueueError):
        await planner.delete(sid, 5)
    await planner.delete(sid, ADMIN)
    with pytest.raises(QueueError):
        await planner.delete(sid, ADMIN)


async def test_remind_two_days_ahead(planner_env):
    db, planner, stream_id, today = planner_env
    thu = today + timedelta(days=2)
    sid = await planner.add(stream_id=stream_id, subject="Физика", first_date=thu, interval_weeks=1,
                            until_date=None, capacity=30, actor_id=ADMIN)
    assert await planner.remind() == 1
    assert planner.bot.sent[0][0] == ADMIN and "Физика" in planner.bot.sent[0][1]
    await planner.skip(sid, thu, ADMIN)
    assert await planner.remind() == 0  # пропущенная дата — без напоминания


async def test_cleanup_removes_old_closed(env, monkeypatch):
    db, service, _, q = env
    await service.close_queue(q)
    planner = SchedulePlanner(FakeBot(), db, service, FakeMessenger(), FakeScheduler(), TZ)
    assert await planner.cleanup() == 0  # закрыта только что
    real_now = repo.now()
    monkeypatch.setattr(repo, "now", lambda: real_now + 30 * 24 * 3600)
    assert await planner.cleanup() == 1
