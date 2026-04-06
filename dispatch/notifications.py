"""Notification dataclass and async priority queue."""

import asyncio
from dataclasses import dataclass, field


@dataclass(order=True)
class Notification:
    priority: int  # 0=urgent, 1=normal (lower = higher priority)
    timestamp: float  # time.time(), breaks ties
    agent_name: str = field(compare=False)
    agent_voice: str = field(compare=False)
    text: str = field(compare=False)


class NotificationQueue:
    """Thin wrapper around asyncio.PriorityQueue."""

    def __init__(self) -> None:
        self._queue: asyncio.PriorityQueue[Notification] = asyncio.PriorityQueue()

    async def put(self, notification: Notification) -> None:
        await self._queue.put(notification)

    def get_nowait(self) -> Notification:
        return self._queue.get_nowait()

    def empty(self) -> bool:
        return self._queue.empty()
