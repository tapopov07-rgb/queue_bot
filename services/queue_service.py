"""Единственная точка изменения очередей. Хендлеры никогда не пишут в queue_entries напрямую.

Очередь — лист с номерами мест 1..capacity. Место можно выбрать (бывают пропуски);
кто-то ушёл — его место пустеет, остальные остаются на своих номерах.
«Порядковый номер» (position) — номер среди занятых мест: «ты 4-й по очереди».

Каждая операция: транзакция под общим write lock + запись в queue_events +
снимок «до/после» (по нему notifier решает, кого уведомить).
"""
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from aiosqlite import Connection, Row

from db import repo
from db.database import Database
from services.permissions import Action, can

log = logging.getLogger(__name__)

MAX_SUBJECT_LEN = 60
MAX_GUEST_NAME_LEN = 40
MAX_ADDED_OTHERS = 1        # сколько других людей один студент может записать в очередь
SWAP_REQUEST_TTL = 30 * 60  # запрос на обмен действует 30 минут
DEFAULT_CAPACITY = 30
MAX_CAPACITY = 90           # сетка выбора места — не больше 100 кнопок (лимит Telegram)


def queue_window(class_date: date, tz: ZoneInfo) -> tuple[int, int, int]:
    """(opens_at, starts_at, ends_at): запись открывается в 00:00 накануне пары
    и закрывается в 23:59 дня пары."""
    opens = datetime.combine(class_date - timedelta(days=1), time(0, 0), tzinfo=tz)
    starts = datetime.combine(class_date, time(0, 0), tzinfo=tz)
    ends = datetime.combine(class_date, time(23, 59, 59), tzinfo=tz)
    return int(opens.timestamp()), int(starts.timestamp()), int(ends.timestamp())


class QueueError(Exception):
    """Ошибка, текст которой можно показать пользователю."""


class _SwapExpired(QueueError):
    """Запрос устарел: пометить его expired нужно отдельной транзакцией,
    потому что транзакция с ошибкой откатывается целиком."""

    def __init__(self, request_id: int) -> None:
        super().__init__("Запрос устарел (действует 30 минут)")
        self.request_id = request_id


@dataclass(frozen=True)
class EntrySnap:
    position: int           # порядковый номер среди занятых мест (1..N)
    slot: int               # номер места в листе
    user_id: int            # владелец записи
    guest_name: str | None  # вписан по имени


@dataclass
class OpResult:
    queue_id: int
    kind: str                      # join | add_classmate | add_by_name | move | leave | remove | swap
    actor_id: int | None
    entry_id: int | None = None
    other_entry_id: int | None = None  # второй участник обмена
    before: dict[int, EntrySnap] = field(default_factory=dict)
    after: dict[int, EntrySnap] = field(default_factory=dict)

    @property
    def position(self) -> int | None:
        snap = self.after.get(self.entry_id) if self.entry_id else None
        return snap.position if snap else None

    @property
    def slot(self) -> int | None:
        snap = self.after.get(self.entry_id) if self.entry_id else None
        return snap.slot if snap else None


class QueueService:
    def __init__(self, db: Database) -> None:
        self.db = db

    # ---------- создание, удаление, статусы ----------

    async def create_queue(self, *, stream_id: int, subject: str, starts_at: int, ends_at: int,
                           opens_at: int, actor_id: int | None, capacity: int = DEFAULT_CAPACITY,
                           schedule_id: int | None = None) -> int:
        subject = subject.strip()
        if not subject or len(subject) > MAX_SUBJECT_LEN:
            raise QueueError(f"Название — от 1 до {MAX_SUBJECT_LEN} символов")
        if not 1 <= capacity <= MAX_CAPACITY:
            raise QueueError(f"Число мест — от 1 до {MAX_CAPACITY}")
        if ends_at <= starts_at or opens_at >= ends_at:
            raise QueueError("Неверное время очереди")
        if ends_at <= repo.now():
            raise QueueError("Этот день уже прошёл")

        status = "open" if opens_at <= repo.now() else "scheduled"
        async with self.db.transaction() as conn:
            # actor_id is None — очередь создаёт планировщик
            if actor_id is not None and not await can(conn, actor_id, Action.CREATE_QUEUE, stream_id=stream_id):
                raise QueueError("Создавать очереди может только ответственный потока")
            queue_id = await repo.create_queue(
                conn, stream_id=stream_id, subject=subject, starts_at=starts_at, ends_at=ends_at,
                opens_at=opens_at, capacity=capacity, status=status, created_by=actor_id,
                schedule_id=schedule_id,
            )
            await repo.log_event(conn, queue_id, actor_id, "create",
                                 {"subject": subject, "starts_at": starts_at, "capacity": capacity})
        return queue_id

    async def delete_queue(self, queue_id: int, actor_id: int) -> tuple[Row, list[Row]]:
        """Удалить очередь целиком. Возвращает очередь и её сообщения в группах
        (их нужно пометить «удалена» — после удаления из БД они уже не найдутся)."""
        async with self.db.transaction() as conn:
            queue = await repo.get_queue(conn, queue_id)
            if queue is None:
                raise QueueError("Очередь уже удалена")
            if not await can(conn, actor_id, Action.DELETE_QUEUE, stream_id=queue["stream_id"]):
                raise QueueError("Удалять очереди может только ответственный потока")
            messages = await repo.get_queue_messages(conn, queue_id)
            await repo.delete_queue(conn, queue_id)
        log.info("Очередь %s «%s» удалена пользователем %s", queue_id, queue["subject"], actor_id)
        return queue, messages

    async def open_queue(self, queue_id: int) -> bool:
        async with self.db.transaction() as conn:
            queue = await repo.get_queue(conn, queue_id)
            if queue is None or queue["status"] != "scheduled":
                return False
            await repo.set_queue_status(conn, queue_id, "open")
            await repo.log_event(conn, queue_id, None, "open")
        return True

    async def close_queue(self, queue_id: int) -> bool:
        async with self.db.transaction() as conn:
            queue = await repo.get_queue(conn, queue_id)
            if queue is None or queue["status"] == "closed":
                return False
            await repo.set_queue_status(conn, queue_id, "closed")
            await repo.expire_queue_swaps(conn, queue_id)
            await repo.log_event(conn, queue_id, None, "close")
        return True

    # ---------- запись ----------

    async def join(self, queue_id: int, user_id: int, slot: int | None = None) -> OpResult:
        """Записаться. slot=None — первое свободное место."""
        async with self.db.transaction() as conn:
            queue = await self._get_open_queue(conn, queue_id)
            existing = await repo.find_entry(conn, queue_id, user_id, guest=False)
            if existing:
                raise QueueError(f"Ты уже в очереди — место {existing['slot']}")
            slot = await self._pick_slot(conn, queue, slot)
            return await self._insert(conn, queue_id, slot, user_id=user_id, guest_name=None, actor_id=user_id)

    async def add_classmate(self, queue_id: int, actor_id: int, classmate_id: int) -> OpResult:
        """Записать одногруппника из списка на первое свободное место. Запись принадлежит ему
        (видит, получает уведомления, может выйти/поменяться); тот, кто записал, тоже может её убрать."""
        if classmate_id == actor_id:
            raise QueueError("Себя записывай кнопкой «Записаться»")
        async with self.db.transaction() as conn:
            queue = await self._get_open_queue(conn, queue_id)
            await self._check_add_limit(conn, queue_id, actor_id)
            existing = await repo.find_entry(conn, queue_id, classmate_id, guest=False)
            if existing:
                raise QueueError(f"Он(а) уже в очереди — место {existing['slot']}")
            slot = await self._pick_slot(conn, queue, None)
            return await self._insert(conn, queue_id, slot, user_id=classmate_id, guest_name=None,
                                      actor_id=actor_id)

    async def add_by_name(self, queue_id: int, actor_id: int, name: str) -> OpResult:
        """Записать человека, которого нет в списке, — просто по имени. Запись принадлежит записавшему."""
        name = " ".join(name.split())
        if not name or len(name) > MAX_GUEST_NAME_LEN:
            raise QueueError(f"Имя — от 1 до {MAX_GUEST_NAME_LEN} символов")
        async with self.db.transaction() as conn:
            queue = await self._get_open_queue(conn, queue_id)
            await self._check_add_limit(conn, queue_id, actor_id)
            slot = await self._pick_slot(conn, queue, None)
            return await self._insert(conn, queue_id, slot, user_id=actor_id, guest_name=name,
                                      actor_id=actor_id)

    async def move(self, entry_id: int, slot: int | None, actor_id: int) -> OpResult:
        """Перейти на другое свободное место. slot=None — первое свободное."""
        async with self.db.transaction() as conn:
            entry = await repo.get_entry(conn, entry_id)
            if entry is None:
                raise QueueError("Этой записи уже нет в очереди")
            queue = await self._get_open_queue(conn, entry["queue_id"])
            # своё место меняет владелец; чужое — модерация (этап 5)
            if entry["user_id"] != actor_id and not await can(
                    conn, actor_id, Action.MOVE, stream_id=queue["stream_id"], entry=entry):
                raise QueueError("Менять место может только владелец записи")
            if slot == entry["slot"]:
                raise QueueError(f"Ты уже на месте {slot}")
            slot = await self._pick_slot(conn, queue, slot)

            before = await self._snapshot(conn, queue["id"])
            await repo.set_slot(conn, entry_id, slot)
            after = await self._snapshot(conn, queue["id"])
            await self._expire_moved_swaps(conn, before, after)
            await repo.log_event(conn, queue["id"], actor_id, "move",
                                 {"entry_id": entry_id, "from": entry["slot"], "to": slot})
        return OpResult(queue["id"], "move", actor_id, entry_id, None, before, after)

    # ---------- выход ----------

    async def leave(self, entry_id: int, actor_id: int) -> OpResult:
        """Убрать конкретную запись (свою или ту, которую сделал сам)."""
        async with self.db.transaction() as conn:
            entry = await repo.get_entry(conn, entry_id)
            if entry is None:
                raise QueueError("Этой записи уже нет в очереди")
            return await self._remove(conn, entry, actor_id)

    async def leave_queue(self, queue_id: int, user_id: int) -> OpResult:
        """Кнопка «Выйти» в группе: убирает свою запись, а если её нет — вписанного по имени."""
        async with self.db.transaction() as conn:
            entry = (await repo.find_entry(conn, queue_id, user_id, guest=False)
                     or await repo.find_entry(conn, queue_id, user_id, guest=True))
            if entry is None:
                raise QueueError("Тебя нет в этой очереди")
            return await self._remove(conn, entry, user_id)

    # ---------- обмен ----------

    async def request_swap(self, from_entry_id: int, to_entry_id: int, actor_id: int) -> int:
        """Создать запрос на обмен. Возвращает id запроса. Предыдущий запрос с этого места отменяется."""
        async with self.db.transaction() as conn:
            src = await repo.get_entry(conn, from_entry_id)
            dst = await repo.get_entry(conn, to_entry_id)
            if src is None or dst is None or src["queue_id"] != dst["queue_id"]:
                raise QueueError("Этой записи уже нет в очереди")
            await self._get_open_queue(conn, src["queue_id"])
            if src["user_id"] != actor_id or not await can(conn, actor_id, Action.SWAP, entry=src):
                raise QueueError("Меняться можно только своим местом")
            if dst["user_id"] == actor_id:
                raise QueueError("Это тоже твоё место")
            if not dst["can_dm"]:
                raise QueueError("У этого человека не открыта личка с ботом — он не сможет "
                                 "подтвердить обмен")
            await repo.cancel_pending_swaps_from(conn, from_entry_id)
            request_id = await repo.create_swap_request(conn, src["queue_id"], from_entry_id, to_entry_id)
            await repo.log_event(conn, src["queue_id"], actor_id, "swap_request",
                                 {"request_id": request_id, "from_entry": from_entry_id, "to_entry": to_entry_id})
        return request_id

    async def accept_swap(self, request_id: int, actor_id: int) -> OpResult:
        async with self._expiring(), self.db.transaction() as conn:
            req, src, dst = await self._get_pending_swap(conn, request_id)
            if dst["user_id"] != actor_id:
                raise QueueError("Этот запрос адресован не тебе")
            await self._get_open_queue(conn, req["queue_id"])

            before = await self._snapshot(conn, req["queue_id"])
            await repo.swap_slots(conn, src, dst)
            await repo.set_swap_status(conn, request_id, "accepted")
            after = await self._snapshot(conn, req["queue_id"])
            await self._expire_moved_swaps(conn, before, after)
            await repo.log_event(conn, req["queue_id"], actor_id, "swap",
                                 {"request_id": request_id, "from_entry": src["id"], "to_entry": dst["id"]})
        return OpResult(req["queue_id"], "swap", actor_id, src["id"], dst["id"], before, after)

    async def decline_swap(self, request_id: int, actor_id: int) -> Row:
        async with self._expiring(), self.db.transaction() as conn:
            req, _, dst = await self._get_pending_swap(conn, request_id)
            if dst["user_id"] != actor_id:
                raise QueueError("Этот запрос адресован не тебе")
            await repo.set_swap_status(conn, request_id, "declined")
        return req

    async def cancel_swap(self, request_id: int, actor_id: int) -> Row:
        async with self._expiring(), self.db.transaction() as conn:
            req, src, _ = await self._get_pending_swap(conn, request_id)
            if src["user_id"] != actor_id:
                raise QueueError("Это не твой запрос")
            await repo.set_swap_status(conn, request_id, "cancelled")
        return req

    # ---------- внутреннее ----------

    async def _get_open_queue(self, conn: Connection, queue_id: int) -> Row:
        queue = await repo.get_queue(conn, queue_id)
        if queue is None:
            raise QueueError("Очередь не найдена")
        if queue["status"] == "scheduled":
            raise QueueError("Запись ещё не открыта")
        if queue["status"] == "closed":
            raise QueueError("Очередь уже закрыта")
        return queue

    async def _pick_slot(self, conn: Connection, queue: Row, slot: int | None) -> int:
        """Проверить выбранное место или найти первое свободное."""
        occupied = await repo.get_occupied_slots(conn, queue["id"])
        if slot is None:
            free = next((s for s in range(1, queue["capacity"] + 1) if s not in occupied), None)
            if free is None:
                raise QueueError("Свободных мест нет")
            return free
        if not 1 <= slot <= queue["capacity"]:
            raise QueueError(f"Места в этой очереди — от 1 до {queue['capacity']}")
        if slot in occupied:
            raise QueueError(f"Место {slot} уже занято")
        return slot

    async def _get_pending_swap(self, conn: Connection, request_id: int) -> tuple[Row, Row, Row]:
        req = await repo.get_swap_request(conn, request_id)
        if req is None or req["status"] != "pending":
            raise QueueError("Запрос уже неактуален")
        if req["created_at"] + SWAP_REQUEST_TTL < repo.now():
            raise _SwapExpired(request_id)
        src = await repo.get_entry(conn, req["from_entry_id"])
        dst = await repo.get_entry(conn, req["to_entry_id"])
        return req, src, dst

    @asynccontextmanager
    async def _expiring(self) -> AsyncIterator[None]:
        try:
            yield
        except _SwapExpired as e:
            async with self.db.transaction() as conn:
                await repo.set_swap_status(conn, e.request_id, "expired")
            raise

    async def _check_add_limit(self, conn: Connection, queue_id: int, actor_id: int) -> None:
        if await repo.count_added_others(conn, queue_id, actor_id) >= MAX_ADDED_OTHERS:
            raise QueueError("Ты уже записал одного человека в эту очередь — больше нельзя")

    async def _snapshot(self, conn: Connection, queue_id: int) -> dict[int, EntrySnap]:
        entries = await repo.get_entries(conn, queue_id)  # по возрастанию номера места
        return {e["id"]: EntrySnap(i, e["slot"], e["user_id"], e["guest_name"])
                for i, e in enumerate(entries, start=1)}

    async def _insert(self, conn: Connection, queue_id: int, slot: int, *, user_id: int,
                      guest_name: str | None, actor_id: int) -> OpResult:
        before = await self._snapshot(conn, queue_id)
        entry_id = await repo.insert_entry(conn, queue_id=queue_id, slot=slot, user_id=user_id,
                                           guest_name=guest_name, added_by=actor_id)
        after = await self._snapshot(conn, queue_id)
        if guest_name:
            kind = "add_by_name"
        elif user_id != actor_id:
            kind = "add_classmate"
        else:
            kind = "join"
        await repo.log_event(conn, queue_id, actor_id, kind,
                             {"entry_id": entry_id, "user_id": user_id, "guest_name": guest_name, "slot": slot})
        return OpResult(queue_id, kind, actor_id, entry_id, None, before, after)

    async def _remove(self, conn: Connection, entry: Row, actor_id: int) -> OpResult:
        queue_id = entry["queue_id"]
        queue = await repo.get_queue(conn, queue_id)
        if queue["status"] == "closed":
            raise QueueError("Очередь уже закрыта")
        action = Action.LEAVE if actor_id in (entry["user_id"], entry["added_by"]) else Action.REMOVE
        if not await can(conn, actor_id, action, stream_id=queue["stream_id"], entry=entry):
            raise QueueError("Это не твоя запись")

        before = await self._snapshot(conn, queue_id)
        await repo.delete_entry(conn, entry["id"])  # место пустеет, остальные не двигаются
        after = await self._snapshot(conn, queue_id)
        await repo.log_event(conn, queue_id, actor_id, action.value,
                             {"entry_id": entry["id"], "user_id": entry["user_id"],
                              "guest_name": entry["guest_name"], "slot": entry["slot"]})
        return OpResult(queue_id, action.value, actor_id, entry["id"], None, before, after)

    async def _expire_moved_swaps(self, conn: Connection, before: dict[int, EntrySnap],
                                  after: dict[int, EntrySnap]) -> None:
        """Запрос на обмен обещал конкретные места — если они поменялись, запрос неактуален."""
        moved = [e for e, s in after.items() if before.get(e) and before[e].slot != s.slot]
        await repo.expire_swaps_for_entries(conn, moved)
