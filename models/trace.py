"""检查器的记录结构：一次触发（Trace）→ 每轮调用（Round）→ 每次 HTTP 尝试（Attempt）。"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Literal


@dataclass(slots=True)
class Attempt:
    """一次实际发出的 HTTP 请求。"""

    method: str
    url: str
    started_at: float
    request_headers: dict[str, str] = field(default_factory=dict)
    request_body: Any = None
    body_truncated: bool = False
    status: int | None = None
    duration_ms: float | None = None
    response_headers: dict[str, str] = field(default_factory=dict)
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "method": self.method,
            "url": self.url,
            "started_at": self.started_at,
            "request_headers": self.request_headers,
            "request_body": self.request_body,
            "body_truncated": self.body_truncated,
            "status": self.status,
            "duration_ms": self.duration_ms,
            "response_headers": self.response_headers,
            "error": self.error,
        }


@dataclass(slots=True)
class Round:
    """一次 LLM 调用（工具循环中的一轮）。"""

    index: int
    kind: Literal["chat", "stream"]
    call_site: str
    provider_id: str
    provider_type: str
    model: str
    started_at: float
    duration_ms: float | None = None
    ttft_ms: float | None = None
    error: str = ""
    usage: dict[str, Any] = field(default_factory=dict)
    response: dict[str, Any] = field(default_factory=dict)
    params: dict[str, Any] = field(default_factory=dict)
    attempts: list[Attempt] = field(default_factory=list)
    wire: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "kind": self.kind,
            "call_site": self.call_site,
            "provider_id": self.provider_id,
            "provider_type": self.provider_type,
            "model": self.model,
            "started_at": self.started_at,
            "duration_ms": self.duration_ms,
            "ttft_ms": self.ttft_ms,
            "error": self.error,
            "usage": self.usage,
            "response": self.response,
            "params": self.params,
            "attempts": [item.to_dict() for item in self.attempts],
            "wire": self.wire,
        }


@dataclass(slots=True)
class Trace:
    """一条用户消息触发的全部调用，或一次旁路调用。"""

    kind: Literal["main", "side"]
    umo: str = ""
    title: str = ""
    sender: str = ""
    layout_id: str = ""
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:16])
    started_at: float = field(default_factory=time.time)
    ended_at: float | None = None
    status: Literal["open", "done", "interrupted"] = "open"
    build: dict[str, Any] = field(default_factory=dict)
    rounds: list[Round] = field(default_factory=list)
    touched_at: float = field(default_factory=time.time)
    # 只在内存中用于识别主 Agent 的调用，不序列化。
    session_id: str = ""
    req_ref: Any = field(default=None, repr=False)

    def summary(self) -> dict[str, Any]:
        """汇总轮数、用量、命中率与耗时。"""

        total_in = sum(int(r.usage.get("input", 0) or 0) for r in self.rounds)
        cached = sum(int(r.usage.get("input_cached", 0) or 0) for r in self.rounds)
        output = sum(int(r.usage.get("output", 0) or 0) for r in self.rounds)
        models = sorted({r.model for r in self.rounds if r.model})
        end = self.ended_at or self.touched_at
        return {
            "id": self.id,
            "kind": self.kind,
            "umo": self.umo,
            "title": self.title,
            "sender": self.sender,
            "layout_id": self.layout_id,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "status": self.status,
            "rounds": len(self.rounds),
            "models": models,
            "input": total_in,
            "input_cached": cached,
            "output": output,
            "hit_rate": round(cached / total_in, 4) if total_in else None,
            "duration_ms": round((end - self.started_at) * 1000, 1),
            "error": any(r.error for r in self.rounds),
        }

    def to_dict(self) -> dict[str, Any]:
        data = self.summary()
        data["build"] = self.build
        data["round_list"] = [item.to_dict() for item in self.rounds]
        return data
