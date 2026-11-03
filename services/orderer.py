"""提示词编排的主流程：基线、钩子追踪、重排写回、统计与预览。"""

from __future__ import annotations

import asyncio
import contextlib
import time
import uuid
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.core.agent.message import TextPart
from astrbot.core.agent.tool import ToolSet

from ..models.layout import Layout
from ..models.segment import HookChange, Run
from .arranger import arrange, match_layout
from .hook_wrapper import HookWrapper, owner_of_module
from .settings import Settings
from .stats import StatsService, recommend, warnings
from .storage import LayoutStore
from .tracker import RawItems, RequestState, Tracker, build_segments, take_snapshot

STATE_KEY = "_prompt_order_state"
SAMPLE_FLUSH_SECONDS = 10.0


def _runs_from(segments: list, separator: str) -> list[Run]:
    """重排写回后，来源表改按新顺序表示，便于继续追踪之后的钩子。"""

    last = len(segments) - 1
    return [
        Run(seg.owner, seg.text.strip() + (separator if i < last else ""))
        for i, seg in enumerate(segments)
    ]


def _toolset(tools: list) -> ToolSet:
    """新建 ToolSet 并直接赋值工具列表。

    ToolSet(tools=...) 会让 pydantic 重新校验每个工具及其 schema，线上 12 个工具约 84ms，
    会阻塞事件循环；先建空集合再赋值只需微秒级，工具对象保持原样。
    """

    toolset = ToolSet(tools=[])
    toolset.tools = list(tools)
    return toolset


def _owner_of_tool(tool: Any) -> str:
    owner = owner_of_module(getattr(tool, "handler_module_path", None))
    if owner:
        return owner
    server = getattr(tool, "mcp_server_name", None)
    return f"MCP:{server}" if server else ""


class OrderService:
    """把各部件串成两个钩子入口（po_begin / po_arrange）。"""

    def __init__(self, data_dir: Path, settings: Settings, own_module: str) -> None:
        self.settings = settings
        self.store = LayoutStore(data_dir)
        self.stats = StatsService(data_dir, settings.stats_window, settings.sample_limit)
        self.tracker = Tracker(_owner_of_tool, settings.diff_char_limit)
        self.wrapper = HookWrapper(own_module, self._before, self._after)
        # 检查器在 po_begin 时创建 Trace，并读取构建阶段记录。
        self.inspector = None
        self._last_flush = 0.0
        self._tasks: set[asyncio.Task] = set()

    def start(self) -> None:
        self.store.load()
        self.stats.open()
        self.wrapper.ensure()

    def stop(self) -> None:
        self.wrapper.uninstall()
        self.stats.close()

    # ---------- 钩子入口 ----------

    def begin(self, event: Any, req: Any) -> None:
        """最先执行：补包装并拍下基线。"""

        self.wrapper.ensure()
        umo = str(getattr(event, "unified_msg_origin", "") or "")
        state = self.tracker.begin(umo, req)
        layout = match_layout(self.store.layouts, umo)
        state.layout_id = layout.id if layout else ""
        event.set_extra(STATE_KEY, state)
        if self.inspector is not None:
            state.trace = self.inspector.open_main(event, state, req)

    def _before(self, event: Any, req: Any):
        state = event.get_extra(STATE_KEY)
        if not isinstance(state, RequestState):
            return None
        return state, take_snapshot(req)

    def _after(self, event: Any, req: Any, token: Any, owner: str, handler: str) -> None:
        state, snap = token
        self.tracker.record(state, snap, req, owner, handler)

    def arrange(self, event: Any, req: Any) -> None:
        """在其他插件之后执行：按布局重排并写回请求。"""

        state = event.get_extra(STATE_KEY)
        if not isinstance(state, RequestState):
            return
        self.tracker.external_check(state, req)
        layout = next((x for x in self.store.layouts if x.id == state.layout_id), None)
        items = self.tracker.raw_items(state, req)
        segments = (
            build_segments(items, layout)
            if layout
            else build_segments(items, Layout(id="default", name="默认"))
        )
        state.before_system = req.system_prompt or ""
        # 重排写回后来源表会按新顺序重建，先留一份含行内改动细节的版本给检查器展示。
        state.before_runs = list(state.runs)
        state.before_tail = [
            {"key": s.key, "owner": s.owner, "text": s.text, "temp": s.temp}
            for s in segments
            if s.zone == "tail"
        ]
        note = ""
        result = None
        if layout is None:
            note = "没有匹配的布局"
        elif not self.settings.enabled or not layout.enabled:
            note = "重排已关闭"
        else:
            result = arrange(
                segments,
                layout,
                original_system=state.before_system,
                make_temp_part=lambda text: TextPart(text=text).mark_as_temp(),
                make_toolset=_toolset,
            )
            if result.error:
                note = result.error
                logger.warning(f"提示词编排：{result.error}，会话={state.umo}")
            elif self.settings.dry_run:
                note = "试运行：只计算不写回"
            elif result.changed:
                if result.system is not None:
                    req.system_prompt = result.system
                    state.runs = _runs_from(result.system_segments, layout.separator)
                if result.parts is not None:
                    req.extra_user_content_parts = result.parts
                    for part, owner in zip(result.parts, result.parts_owners, strict=True):
                        if id(part) not in state.parts:
                            self.tracker.own_part(state, part, owner)
                if result.tools is not None:
                    req.func_tool = result.tools
                note = "已按布局重排"
            else:
                note = "顺序已符合布局，未改动"
            for text in result.violations:
                state.changes.append(HookChange("prompt_order", "arrange", "tail", text))
        state.arranged = True
        state.arrange_note = note
        state.after_system = req.system_prompt or ""
        after_parts = list(req.extra_user_content_parts or [])
        state.after_tail = [
            {"text": getattr(p, "text", "") if not isinstance(p, dict) else p.get("text", "")}
            for p in after_parts
        ]
        if layout is not None:
            # SQLite 提交可能耗时几十毫秒，放到线程里，不阻塞事件循环。
            self._background(self._observe, layout.id, segments)
        self.stats.add_sample(self._sample(event, state, items, result))
        now = time.monotonic()
        if now - self._last_flush >= SAMPLE_FLUSH_SECONDS:
            self._last_flush = now
            self._background(self.stats.flush_samples)

    def _background(self, fn, *args) -> None:
        task = asyncio.create_task(asyncio.to_thread(fn, *args))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _observe(self, layout_id: str, segments: list) -> None:
        try:
            self.stats.observe(layout_id, segments)
        except Exception:
            logger.warning("提示词编排：统计写入失败", exc_info=True)

    def finish(self, event: Any) -> None:
        """本轮结束：把重排之后发生的改动补进样本。"""

        state = event.get_extra(STATE_KEY)
        if not isinstance(state, RequestState):
            return
        if any(c.after_arrange for c in state.changes):
            self.stats.update_sample_changes(id(state), [c.to_dict() for c in state.changes])

    def _sample(self, event: Any, state: RequestState, items: RawItems, result) -> dict[str, Any]:
        message = ""
        with contextlib.suppress(Exception):
            message = str(event.get_message_str() or "")[:80]
        return {
            "id": uuid.uuid4().hex[:12],
            "state_id": id(state),
            "ts": time.time(),
            "umo": state.umo,
            "layout_id": state.layout_id,
            "title": message,
            "items": items.to_dict(),
            "changes": [c.to_dict() for c in state.changes],
            "degraded": list(state.degraded),
            "arranged": bool(
                result and result.changed and not self.settings.dry_run and not result.error
            ),
            "note": state.arrange_note,
            "plan": result.plan if result else {},
        }

    # ---------- 页面接口用 ----------

    def preview(self, raw_layout: Any, sample_id: str) -> dict[str, Any]:
        layout = Layout.from_dict(raw_layout)
        sample = self.stats.sample(sample_id)
        if sample is None:
            raise LookupError("样本不存在或已被新样本覆盖")
        items = RawItems.from_dict(sample["items"])
        segments = build_segments(items, layout)
        before_system = "".join(r.text for r in items.runs)
        result = arrange(
            segments,
            layout,
            original_system=before_system,
            make_temp_part=lambda text: {"text": text, "temp": True, "moved": True},
            make_toolset=lambda tools: tools,
        )
        after_system = before_system if result.system is None else result.system
        tail_parts = (
            [s.ref for s in segments if s.zone == "tail"] if result.parts is None else result.parts
        )
        by_key = {s.key: s for s in segments}
        return {
            "segments": [s.to_dict() for s in segments],
            "before": {
                "system": [s.key for s in segments if s.zone == "system"],
                "tail": [s.key for s in segments if s.zone == "tail"],
                "system_text": before_system,
            },
            "after": {
                "system": result.plan.get("system", []),
                "tail": result.plan.get("tail", []),
                "tools": result.plan.get("tools", []),
                "system_text": after_system,
                "tail_text": [
                    p.get("text", "") if isinstance(p, dict) else str(p) for p in tail_parts
                ],
            },
            "changed": result.changed,
            "violations": result.violations,
            "error": result.error,
            "owners": sorted({s.owner for s in by_key.values()}),
        }

    def layout_report(self, raw_layout: Any) -> dict[str, Any]:
        layout = Layout.from_dict(raw_layout)
        stats = self.stats.segment_stats(layout.id)
        samples = [s for s in self.stats.all_samples() if s.get("layout_id") == layout.id]
        return {"warnings": warnings(layout, stats, samples), "stats": stats}

    def recommend(self, raw_layout: Any) -> dict[str, Any]:
        layout = Layout.from_dict(raw_layout)
        return recommend(layout, self.stats.segment_stats(layout.id)).to_dict()
