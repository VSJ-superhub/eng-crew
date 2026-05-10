import asyncio
from collections import defaultdict


class EventBus:
    def __init__(self):
        self._global_subscribers: list[asyncio.Queue] = []
        self._run_subscribers: dict[str, list[asyncio.Queue]] = defaultdict(list)

    async def publish_global(self, event: dict) -> None:
        dead = []
        for q in self._global_subscribers:
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                dead.append(q)
        for q in dead:
            self._global_subscribers.remove(q)

    async def publish_run_event(self, run_id: str, event: dict) -> None:
        dead = []
        for q in self._run_subscribers[run_id]:
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                dead.append(q)
        for q in dead:
            self._run_subscribers[run_id].remove(q)

    def subscribe_global(self, maxsize: int = 100) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=maxsize)
        self._global_subscribers.append(q)
        return q

    def unsubscribe_global(self, q: asyncio.Queue) -> None:
        try:
            self._global_subscribers.remove(q)
        except ValueError:
            pass

    def subscribe_run(self, run_id: str, maxsize: int = 100) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=maxsize)
        self._run_subscribers[run_id].append(q)
        return q

    def unsubscribe_run(self, run_id: str, q: asyncio.Queue) -> None:
        try:
            self._run_subscribers[run_id].remove(q)
        except ValueError:
            pass
        if not self._run_subscribers[run_id]:
            del self._run_subscribers[run_id]


event_bus = EventBus()
