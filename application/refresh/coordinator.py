"""与具体上游和存储无关的账号级 singleflight 协调器。"""

import asyncio
from collections.abc import Awaitable, Callable
from typing import Generic, TypeVar

_T = TypeVar("_T")


class SingleFlightCoordinator(Generic[_T]):
    """让同一复合账号的并发调用共享一个任务，同时保护等待者取消。"""

    def __init__(self) -> None:
        self._guard = asyncio.Lock()
        self._tasks: dict[str, asyncio.Task[_T]] = {}

    async def run(self, key: str, factory: Callable[[], Awaitable[_T]]) -> _T:
        async with self._guard:
            task = self._tasks.get(key)
            if task is None or task.done():
                task = asyncio.create_task(factory())
                self._tasks[key] = task
        try:
            return await asyncio.shield(task)
        finally:
            if task.done():
                async with self._guard:
                    if self._tasks.get(key) is task:
                        self._tasks.pop(key, None)

    async def wait(self) -> None:
        async with self._guard:
            tasks = tuple(self._tasks.values())
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
