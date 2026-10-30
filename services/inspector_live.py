"""检查器的实时推送：每个 SSE 连接一个有界队列，慢连接丢最旧的事件。"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator
from typing import Any

QUEUE_SIZE = 256
HEARTBEAT_SECONDS = 15


class LiveHub:
    """只推摘要；详情由页面按 ID 拉取。必须在事件循环线程中调用。"""

    def __init__(self) -> None:
        self._subs: set[asyncio.Queue] = set()
        self._closed = False

    @property
    def subscribers(self) -> int:
        return len(self._subs)

    def publish(self, kind: str, data: dict[str, Any]) -> None:
        if self._closed:
            return
        message = {"type": kind, **data}
        for queue in list(self._subs):
            if queue.full():
                with contextlib.suppress(asyncio.QueueEmpty):
                    queue.get_nowait()
                # 丢过事件就通知页面重拉列表。
                message_gap = {"type": "gap"}
                try:
                    queue.put_nowait(message_gap)
                except asyncio.QueueFull:
                    continue
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait(message)

    async def stream(self) -> AsyncIterator[str]:
        queue: asyncio.Queue = asyncio.Queue(QUEUE_SIZE)
        self._subs.add(queue)
        try:
            yield _frame({"type": "hello"})
            while not self._closed:
                try:
                    item = await asyncio.wait_for(queue.get(), HEARTBEAT_SECONDS)
                except asyncio.TimeoutError:
                    item = {"type": "heartbeat"}
                if item is None:
                    break
                yield _frame(item)
        finally:
            self._subs.discard(queue)

    def close(self) -> None:
        self._closed = True
        for queue in list(self._subs):
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait(None)


def _frame(data: dict[str, Any]) -> str:
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"
