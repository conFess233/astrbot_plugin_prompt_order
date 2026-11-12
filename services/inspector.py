"""检查器总协调：开关与过滤、记录的开启与结束、实时事件、费用估算。"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Iterable
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any

from astrbot.api import logger

from ..models.trace import Attempt, Round, Trace
from .inspector_capture import CURRENT_TRACE, ProviderCapture
from .inspector_live import LiveHub
from .inspector_store import TraceStore
from .settings import Settings

REFRESH_SECONDS = 60.0
IDLE_CLOSE_SECONDS = 300.0
# 还有一轮没返回（例如长时间思考的流式调用）时放宽到半小时。
RUNNING_CLOSE_SECONDS = 1800.0


class InspectorService:
    def __init__(self, data_dir: Path, settings: Settings, context: Any) -> None:
        self.settings = settings
        self.context = context
        self.live = LiveHub()
        self.store = TraceStore(data_dir / "inspector", settings.inspector.memory_traces)
        self.capture = ProviderCapture(self)
        self._task: asyncio.Task | None = None
        self.apply_settings(settings)

    # ---------- 生命周期 ----------

    def start(self) -> None:
        self.store.open()
        self.store.start()
        self.refresh()
        self._task = asyncio.create_task(self._maintain())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        self.capture.uninstall()
        self.live.close()
        await self.store.stop()

    def apply_settings(self, settings: Settings) -> None:
        self.settings = settings
        ins = settings.inspector
        self.store.persist = ins.persist
        self.store.retention_days = ins.retention_days
        self.store.retention_mb = ins.retention_mb
        self.store.memory_limit = ins.memory_traces

    def refresh(self) -> int:
        try:
            providers = list(self.context.get_all_providers() or [])
        except Exception:
            logger.warning("提示词编排：读取 provider 列表失败", exc_info=True)
            return 0
        return self.capture.refresh(providers)

    async def _maintain(self) -> None:
        last_refresh = time.monotonic()
        while True:
            await asyncio.sleep(30)
            try:
                if time.monotonic() - last_refresh >= REFRESH_SECONDS:
                    # 旁路调用不经过 po_begin，靠定期检查覆盖新建或重建的 provider。
                    last_refresh = time.monotonic()
                    self.refresh()
                now = time.time()
                for trace in self.store.open_traces():
                    running = any(r.duration_ms is None for r in trace.rounds)
                    limit = RUNNING_CLOSE_SECONDS if running else IDLE_CLOSE_SECONDS
                    if now - trace.touched_at > limit:
                        # 出错路径可能收不到 on_agent_done，长时间无动静就收尾。
                        trace.status = "interrupted"
                        self._close(trace)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("提示词编排：检查器维护任务出错", exc_info=True)

    # ---------- 过滤 ----------

    def capturing(self) -> bool:
        return self.settings.inspector.enabled

    def _wanted(self, umo: str) -> bool:
        ins = self.settings.inspector
        if not ins.enabled:
            return False
        if ins.umo_include and not any(fnmatchcase(umo, p) for p in ins.umo_include):
            return False
        return not any(fnmatchcase(umo, p) for p in ins.umo_exclude)

    # ---------- 记录开启与结束 ----------

    def open_main(self, event: Any, state: Any, req: Any) -> Trace | None:
        umo = state.umo
        # 清除历史思考、省略历史图片也依赖 provider 包装，不记录的会话同样要补装。
        self.refresh()
        if not self._wanted(umo):
            CURRENT_TRACE.set(None)
            return None
        title, sender = "", ""
        try:
            title = str(event.get_message_str() or "")[:120]
            sender = str(event.get_sender_name() or event.get_sender_id() or "")
        except Exception:
            pass
        trace = Trace(kind="main", umo=umo, title=title, sender=sender, layout_id=state.layout_id)
        trace.session_id = str(getattr(req, "session_id", "") or "")
        trace.req_ref = req
        self.store.add(trace)
        CURRENT_TRACE.set(trace)
        self.live.publish("trace_start", trace.summary())
        return trace

    def open_side(self, site: str) -> Trace | None:
        if not self.settings.inspector.enabled or not self.settings.inspector.capture_side:
            return None
        trace = Trace(kind="side", title=f"旁路调用 · {site}")
        self.store.add(trace)
        self.live.publish("trace_start", trace.summary())
        return trace

    def close_main(self, state: Any) -> None:
        trace = getattr(state, "trace", None)
        if trace is None or trace.status != "open":
            return
        trace.build = build_record(state)
        self._close(trace)

    def _close(self, trace: Trace) -> None:
        trace.ended_at = time.time()
        trace.req_ref = None
        self.store.finish(trace)
        self.live.publish("trace_end", self.summary(trace))

    def summary(self, trace: Trace) -> dict[str, Any]:
        data = trace.summary()
        data["cost"] = self.cost(trace)
        return data

    # ---------- 轮次回调（由截获层调用） ----------

    def round_started(self, trace: Trace, rnd: Round) -> None:
        self.live.publish(
            "round_start",
            {
                "trace_id": trace.id,
                "index": rnd.index,
                "model": rnd.model,
                "provider_id": rnd.provider_id,
            },
        )

    def round_finished(self, trace: Trace, rnd: Round) -> None:
        self.live.publish(
            "round_end",
            {
                "trace_id": trace.id,
                "index": rnd.index,
                "usage": {k: v for k, v in rnd.usage.items() if k != "raw"},
                "duration_ms": rnd.duration_ms,
                "error": rnd.error,
            },
        )
        if trace.kind == "side":
            self._close(trace)

    def attempt_event(self, rnd: Round, attempt: Attempt) -> None:
        self.live.publish(
            "attempt", {"index": rnd.index, "url": attempt.url, "method": attempt.method}
        )

    # ---------- 费用 ----------

    def cost(self, trace: Trace) -> float | None:
        return self.cost_of((rnd.model, rnd.usage) for rnd in trace.rounds)

    def cost_of(self, rounds: Iterable[tuple[str, dict[str, Any]]]) -> float | None:
        """按每轮的模型与用量估算费用；没有配置价格的模型不计入。"""

        total, priced = 0.0, False
        for model, usage in rounds:
            price = self.settings.inspector.price_of(model)
            if price is None:
                continue
            miss, hit, out = price
            cached = usage.get("input_cached", 0)
            other = usage.get("input_other", usage.get("input", 0) - cached)
            total += (other * miss + cached * hit + usage.get("output", 0) * out) / 1_000_000
            priced = True
        return round(total, 6) if priced else None

    def status(self) -> dict[str, Any]:
        return {
            "enabled": self.settings.inspector.enabled,
            "depth": self.settings.inspector.depth,
            "capture_side": self.settings.inspector.capture_side,
            "persist": self.settings.inspector.persist,
            "providers": dict(self.capture.wire_modes),
            "subscribers": self.live.subscribers,
            "open_traces": len(self.store.open_traces()),
        }


def build_record(state: Any) -> dict[str, Any]:
    """构建阶段（L1）的记录：来源表、钩子改动、重排前后。"""

    return {
        "layout_id": state.layout_id,
        "note": state.arrange_note,
        "degraded": list(state.degraded),
        "runs": [
            {"owner": r.owner, "mark": r.mark, "text": r.text}
            for r in (state.before_runs or state.runs)
        ],
        "changes": [c.to_dict() for c in state.changes],
        "before_system": state.before_system,
        "after_system": state.after_system,
        "before_tail": state.before_tail,
        "after_tail": state.after_tail,
        "compact": getattr(state, "compact", None),
        "rewrite": getattr(state, "rewrite", None),
    }
