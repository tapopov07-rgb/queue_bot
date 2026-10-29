"""Единственная точка изменения очередей. Хендлеры никогда не пишут в queue_entries напрямую.

Каждая операция: транзакция под общим write lock + запись в queue_events +
снимок позиций «до/после» (по нему notifier решает, кого уведомить).
"""
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

from aiosqlite import Connection, Row

from db import repo
from db.database import Database
from services.permissions import Action, can

MAX_SUBJECT_LEN = 60
MAX_GUEST_NAME_LEN = 40
MAX_ADDED_OTHERS = 1        # сколько других людей один студент может записать в очередь
SWAP_REQUEST_TTL = 30 * 60  # запрос на обмен действует 30 минут


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
    position: int
    user_id: int            # владелец записи
    guest_name: str | None  # вписан по имени


@dataclass
class OpResult:
    queue_id: int
    kind: str                      # join | add_classmate | add_by_name | leave | remove | swap
    actor_id: int | None
    entry_id: int | None = None
    other_entry_id: int | None = None  # второй участник обмена
    before: dict[int, EntrySnap] = field(default_factory=dict)
    after: dict[int, EntrySnap] = field(default_factory=dict)

    @property
    def position(self) -> int | None:
        snap = self.after.get(self.entry_id) if self.entry_id else None
        return snap.position if snap else None


class QueueService:
    def __init__(self, db: Database) -> None:
        self.db = db

    # ---------- создание и статусы ----------

    async def create_queue(self, *, stream_id: int, subject: str, starts_at: int, ends_at: int,
                           opens_at: int, actor_id: int | None, schedule_id: int | None = None) -> int:
        subject = subject.strip()
        if not subject or len(subject) > MAX_SUBJECT_LEN:
            raise QueueError(f"Название — от 1 до {MAX_SUBJECT_LEN} символов")
        if ends_at <= starts_at:
            raise QueueError("Пара должна заканчиваться позже, чем начинается")
        if opens_at >= ends_at:
            raise QueueError("Запись должна открыться до конца пары")
        if ends_at <= repo.now():
            raise QueueError("Эта пара уже закончилась")

        status = "open" if opens_at <= repo.now() else "scheduled"
        async with self.db.transaction() as conn:
            # actor_id is None — очередь создаёт планировщик
            if actor_id is not None and not await can(conn, actor_id, Action.CREATE_QUEUE, stream_id=stream_id):
                raise QueueError("Создавать очереди может только ответственный потока")
            queue_id = await repo.create_queue(
                conn, stream_id=stream_id, subject=subject, starts_at=starts_at, ends_at=ends_at,
                opens_at=opens_at, status=status, created_by=actor_id, schedule_id=schedule_id,
            )
            await repo.log_event(conn, queue_id, actor_id, "create",
                                 {"subject": subject, "starts_at": starts_at, "opens_at": opens_at})
        return queue_id

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

    async def join(self, queue_id: int, user_id: int) -> OpResult:
        async with self.db.transaction() as conn:
            await self._get_open_queue(conn, queue_id)
            existing = await repo.find_entry(conn, queue_id, user_id, guest=False)
            if existing:
                raise QueueError(f"Ты уже в очереди — место {existing['position']}")
            return await self._append(conn, queue_id, user_id=user_id, guest_name=None, actor_id=user_id)

    async def add_classmate(self, queue_id: int, actor_id: int, classmate_id: int) -> OpResult:
        """Записать одногруппника из списка. Запись принадлежит ему (видит, получает уведомления,
        может выйти/поменяться); тот, кто записал, тоже может её убрать."""
        if classmate_id == actor_id:
            raise QueueError("Себя записывай кнопкой «Записаться»")
        async with self.db.transaction() as conn:
            await self._get_open_queue(conn, queue_id)
            await self._check_add_limit(conn, queue_id, actor_id)
            existing = await repo.find_entry(conn, queue_id, classmate_id, guest=False)
            if existing:
                raise QueueError(f"Он(а) уже в очереди — место {existing['position']}")
            return await self._append(conn, queue_id, user_id=classmate_id, guest_name=None,
                                      actor_id=actor_id)

    async def add_by_name(self, queue_id: int, actor_id: int, name: str) -> OpResult:
        """Записать человека, которого нет в списке, — просто по имени. Запись принадлежит записавшему."""
        name = " ".join(name.split())
        if not name or len(name) > MAX_GUEST_NAME_LEN:
            raise QueueError(f"Имя — от 1 до {MAX_GUEST_NAME_LEN} символов")
        async with self.db.transaction() as conn:
            await self._get_open_queue(conn, queue_id)
            await self._check_add_limit(conn, queue_id, actor_id)
            return await self._append(conn, queue_id, user_id=actor_id, guest_name=name,
                                      actor_id=actor_id)

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
            order = self._order(before)
            i, j = order.index(src["id"]), order.index(dst["id"])
            order[i], order[j] = order[j], order[i]
            await self._reposition(conn, order)
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
        return {e["id"]: EntrySnap(e["position"], e["user_id"], e["guest_name"])
                for e in await repo.get_entries(conn, queue_id)}

    @staticmethod
    def _order(snapshot: dict[int, EntrySnap]) -> list[int]:
        return [e for e, _ in sorted(snapshot.items(), key=lambda kv: kv[1].position)]

    async def _append(self, conn: Connection, queue_id: int, *, user_id: int,
                      guest_name: str | None, actor_id: int) -> OpResult:
        before = await self._snapshot(conn, queue_id)
        entry_id = await repo.insert_entry(
            conn, queue_id=queue_id, position=len(before) + 1, user_id=user_id,
            guest_name=guest_name, added_by=actor_id,
        )
        after = await self._snapshot(conn, queue_id)
        if guest_name:
            kind = "add_by_name"
        elif user_id != actor_id:
            kind = "add_classmate"
        else:
            kind = "join"
        await repo.log_event(conn, queue_id, actor_id, kind,
                             {"entry_id": entry_id, "user_id": user_id, "guest_name": guest_name,
                              "position": after[entry_id].position})
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
        await repo.delete_entry(conn, entry["id"])  # запросы на обмен с ней удалятся каскадом
        await self._reposition(conn, [e for e in self._order(before) if e != entry["id"]])
        after = await self._snapshot(conn, queue_id)
        await self._expire_moved_swaps(conn, before, after)
        await repo.log_event(conn, queue_id, actor_id, action.value,
                             {"entry_id": entry["id"], "user_id": entry["user_id"],
                              "guest_name": entry["guest_name"], "position": before[entry["id"]].position})
        return OpResult(queue_id, action.value, actor_id, entry["id"], None, before, after)

    async def _reposition(self, conn: Connection, ordered_entry_ids: list[int]) -> None:
        """Единая функция расстановки мест 1..N. Её используют выход, обмен,
        а дальше удаление и перестановка (этап 5)."""
        await repo.set_positions(conn, [(e, i) for i, e in enumerate(ordered_entry_ids, start=1)])

    async def _expire_moved_swaps(self, conn: Connection, before: dict[int, EntrySnap],
                                  after: dict[int, EntrySnap]) -> None:
        """Запрос на обмен обещал конкретные места — если они сдвинулись, запрос неактуален."""
        moved = [e for e, s in after.items() if before.get(e) and before[e].position != s.position]
        await repo.expire_swaps_for_entries(conn, moved)
