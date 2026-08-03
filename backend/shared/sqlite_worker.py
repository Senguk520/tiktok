"""Small polling loop for database-backed workers.

The loop stores no business object between iterations. Claiming, leases and
recovery remain transactional facts in SQLite; the event only controls process
shutdown and is not a result cache.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Generic, TypeVar

T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class PollingPolicy:
    idle_seconds: float = 1.0
    error_seconds: float = 5.0

    def __post_init__(self) -> None:
        if self.idle_seconds <= 0 or self.error_seconds <= 0:
            raise ValueError("polling delays must be positive")


class SQLitePollingWorker(Generic[T]):
    def __init__(
        self,
        *,
        claim: Callable[[], Awaitable[Sequence[T]]],
        handle: Callable[[T], Awaitable[None]],
        policy: PollingPolicy | None = None,
        on_error: Callable[[Exception], Awaitable[None]] | None = None,
    ) -> None:
        self._claim = claim
        self._handle = handle
        self._policy = PollingPolicy() if policy is None else policy
        self._on_error = on_error

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                claimed = await self._claim()
                for item in claimed:
                    if stop.is_set():
                        break
                    await self._handle(item)
                delay = 0.0 if claimed else self._policy.idle_seconds
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if self._on_error is not None:
                    await self._on_error(exc)
                delay = self._policy.error_seconds
            if delay > 0:
                try:
                    await asyncio.wait_for(stop.wait(), timeout=delay)
                except TimeoutError:
                    pass