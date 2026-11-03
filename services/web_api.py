"""管理页接口：布局编辑、统计与预览、检查器查询和实时推送。"""

from __future__ import annotations

import asyncio
import functools
import json
import time
from collections.abc import Callable
from typing import Any

from astrbot.api import logger
from astrbot.api.web import error_response, json_response, request, stream_response

from ..models.layout import LayoutError
from .inspector import InspectorService
from .orderer import OrderService
from .settings import apply_inspector_patch, apply_top_patch
from .storage import StaleVersionError

PLUGIN_NAME = "astrbot_plugin_prompt_order"
MAX_LIMIT = 200


def guarded(fn: Callable) -> Callable:
    """把业务异常转换为带说明的错误响应，未知异常记录堆栈后返回 500。"""

    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        try:
            return await fn(*args, **kwargs)
        except (LayoutError, ValueError) as exc:
            return error_response(str(exc), status_code=400)
        except StaleVersionError as exc:
            return error_response(str(exc), status_code=409)
        except LookupError as exc:
            return error_response(str(exc), status_code=404)
        except Exception:
            logger.exception(f"提示词编排：接口处理失败，路由={fn.__name__}")
            return error_response("服务端处理失败，详情见 AstrBot 日志", status_code=500)

    return wrapper


def _int_arg(name: str, default: int, low: int, high: int) -> int:
    value = request.query.get(name, default, type=int)
    return max(low, min(high, value))


class WebApi:
    def __init__(
        self,
        context: Any,
        config: Any,
        order: OrderService,
        inspector: InspectorService,
        on_settings_changed: Callable[[], None],
    ) -> None:
        self.context = context
        self.config = config
        self.order = order
        self.inspector = inspector
        self._settings_changed = on_settings_changed
        self._save_lock = asyncio.Lock()

    def register(self) -> None:
        routes = (
            ("state", self.state, ["GET"], "读取布局、开关与包装状态"),
            ("settings", self.update_settings, ["POST"], "修改重排、试运行与清除历史思考开关"),
            ("report", self.report, ["POST"], "布局草稿的统计与警告"),
            ("samples", self.samples, ["GET"], "最近请求样本列表"),
            ("sample", self.sample, ["GET"], "单个样本的原始条目与改动记录"),
            ("preview", self.preview, ["POST"], "用布局草稿重排样本"),
            ("recommend", self.recommend, ["POST"], "生成推荐布局"),
            ("save", self.save, ["POST"], "保存布局为新版本"),
            ("versions", self.versions, ["GET"], "布局历史版本"),
            ("rollback", self.rollback, ["POST"], "回滚到历史版本"),
            ("inspector/status", self.inspector_status, ["GET"], "检查器状态"),
            ("inspector/config", self.inspector_config, ["POST"], "修改检查器开关"),
            ("inspector/traces", self.traces, ["GET"], "调用记录列表"),
            ("inspector/trace", self.trace, ["GET"], "单条调用记录"),
            ("inspector/stats", self.stats, ["GET"], "用量汇总"),
            ("inspector/live", self.live, ["GET"], "实时事件流"),
            ("inspector/export", self.export, ["GET"], "导出调用记录"),
            ("inspector/clear", self.clear, ["POST"], "清空调用记录"),
        )
        for route, handler, methods, desc in routes:
            self.context.register_web_api(f"/{PLUGIN_NAME}/{route}", handler, methods, desc)

    # ---------- 排序 ----------

    @guarded
    async def state(self):
        settings = self.order.settings
        return json_response(
            {
                "enabled": settings.enabled,
                "dry_run": settings.dry_run,
                "strip_reasoning": settings.strip_reasoning,
                "config": self.order.store.snapshot(),
                "wrapped_hooks": len(self.order.wrapper._wrapped),
            }
        )

    @guarded
    async def update_settings(self):
        changed = apply_top_patch(self.config, await request.json(default={}))
        if changed:
            self._settings_changed()
        return json_response({"changed": changed})

    @guarded
    async def report(self):
        body = await request.json(default={})
        data = await asyncio.to_thread(self.order.layout_report, body.get("layout"))
        return json_response(data)

    @guarded
    async def samples(self):
        layout_id = str(request.query.get("layout", "") or "")
        limit = _int_arg("limit", 30, 1, MAX_LIMIT)
        return json_response({"items": self.order.stats.sample_list(layout_id, limit)})

    @guarded
    async def sample(self):
        sample = self.order.stats.sample(str(request.query.get("id", "")))
        if sample is None:
            raise LookupError("样本不存在或已被新样本覆盖")
        return json_response(sample)

    @guarded
    async def preview(self):
        body = await request.json(default={})
        data = await asyncio.to_thread(
            self.order.preview, body.get("layout"), str(body.get("sample_id", ""))
        )
        return json_response(data)

    @guarded
    async def recommend(self):
        body = await request.json(default={})
        data = await asyncio.to_thread(self.order.recommend, body.get("layout"))
        return json_response({"layout": data})

    @guarded
    async def save(self):
        body = await request.json(default={})
        base = body.get("base_version")
        if not isinstance(base, int):
            raise ValueError("缺少 base_version")
        async with self._save_lock:
            version = await asyncio.to_thread(
                self.order.store.save, body.get("layouts"), base, str(body.get("label", ""))
            )
        logger.info(f"提示词编排：布局已保存为 v{version}，操作者={request.username}")
        return json_response({"version": version, "config": self.order.store.snapshot()})

    @guarded
    async def versions(self):
        return json_response({"items": await asyncio.to_thread(self.order.store.versions)})

    @guarded
    async def rollback(self):
        body = await request.json(default={})
        version, base = body.get("version"), body.get("base_version")
        if not isinstance(version, int) or not isinstance(base, int):
            raise ValueError("缺少 version 或 base_version")
        async with self._save_lock:
            new_version = await asyncio.to_thread(self.order.store.rollback, version, base)
        logger.info(f"提示词编排：已回滚到 v{version}，生成 v{new_version}")
        return json_response({"version": new_version, "config": self.order.store.snapshot()})

    # ---------- 检查器 ----------

    @guarded
    async def inspector_status(self):
        data = self.inspector.status()
        data["disk_bytes"] = await asyncio.to_thread(self.inspector.store.disk_usage)
        data["settings"] = dict(self.config.get("inspector") or {})
        return json_response(data)

    @guarded
    async def inspector_config(self):
        changed = apply_inspector_patch(self.config, await request.json(default={}))
        if changed:
            self._settings_changed()
        return json_response({"changed": changed})

    def _filters(self) -> dict[str, Any]:
        q = request.query
        return {
            "since": q.get("since", None, type=float),
            "until": q.get("until", None, type=float),
            "kind": q.get("kind", "") if q.get("kind", "") in ("main", "side") else "",
            "umo": str(q.get("umo", "") or "")[:200],
            "model": str(q.get("model", "") or "")[:200],
            "errors": q.get("errors", "") == "1",
            "low_hit": q.get("low_hit", "") == "1",
            "min_tokens": q.get("min_tokens", 0, type=int),
            "q": str(q.get("q", "") or "")[:100],
        }

    @guarded
    async def traces(self):
        filters = self._filters()
        before = request.query.get("before", None, type=float)
        limit = _int_arg("limit", 50, 1, MAX_LIMIT)
        store = self.inspector.store
        # 内存中的记录只在事件循环线程读取；线程里只做数据库查询。
        memory = store.memory_summaries()
        items = await asyncio.to_thread(store.query, filters, limit, before, memory)
        for item in items:
            trace = self.inspector.store.recent.get(item["id"])
            if trace is not None:
                item["cost"] = self.inspector.cost(trace)
        return json_response({"items": items})

    @guarded
    async def trace(self):
        trace_id = str(request.query.get("id", ""))
        store = self.inspector.store
        trace = store.recent.get(trace_id)
        if trace is not None:
            data = trace.to_dict()
        else:
            data = await asyncio.to_thread(store.get_persisted, trace_id)
        if data is None:
            raise LookupError("记录不存在或已按保留策略删除")
        data["cost"] = self.inspector.cost_of(
            (str(r.get("model", "")), r.get("usage") or {}) for r in data.get("round_list", [])
        )
        return json_response(data)

    @guarded
    async def stats(self):
        # 前端按浏览器本地时间算好 since/until；只给 days 时沿用旧行为。
        since = request.query.get("since", None, type=float)
        until = request.query.get("until", None, type=float)
        if since is None:
            days = _int_arg("days", 1, 1, 90)
            since = time.time() - days * 86400 if days > 1 else _today_start()
        if until is not None and until <= since:
            raise ValueError("结束时间必须晚于开始时间")
        tz = request.query.get("tz", None, type=int)
        if tz is not None and not -900 <= tz <= 900:
            raise ValueError("时区偏移超出范围")
        store = self.inspector.store
        memory = store.memory_summaries()
        return json_response(await asyncio.to_thread(store.stats, since, memory, until, tz))

    async def live(self):
        return stream_response(
            self.inspector.live.stream(),
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @guarded
    async def export(self):
        limit = _int_arg("limit", 200, 1, 2000)
        store = self.inspector.store
        summaries = await asyncio.to_thread(
            store.query, self._filters(), limit, None, store.memory_summaries()
        )
        # 内存记录在事件循环线程序列化；其余的一次性放进线程读盘，不逐条切换线程。
        lines: list[str | None] = []
        missing: list[tuple[int, str]] = []
        for summary in summaries:
            trace = store.recent.get(summary["id"])
            if trace is not None:
                lines.append(json.dumps(trace.to_dict(), ensure_ascii=False, default=str))
            else:
                missing.append((len(lines), summary["id"]))
                lines.append(None)

        def read_missing() -> list[tuple[int, str]]:
            found = []
            for index, trace_id in missing:
                data = store.get_persisted(trace_id)
                if data is not None:
                    found.append((index, json.dumps(data, ensure_ascii=False, default=str)))
            return found

        for index, line in await asyncio.to_thread(read_missing):
            lines[index] = line
        return stream_response(
            iter([line + "\n" for line in lines if line is not None]),
            content_type="application/x-ndjson; charset=utf-8",
            headers={"Content-Disposition": 'attachment; filename="prompt_order_traces.jsonl"'},
        )

    @guarded
    async def clear(self):
        body = await request.json(default={})
        if body.get("confirm") is not True:
            raise ValueError("清空需要确认")
        # 已落盘的记录同时在内存里，取两者较大值作为清除条数，避免重复计数。
        in_memory = self.inspector.store.clear_memory()
        count = max(in_memory, await asyncio.to_thread(self.inspector.store.clear_disk))
        logger.info(f"提示词编排：已清空 {count} 条调用记录，操作者={request.username}")
        return json_response({"cleared": count})


def _today_start() -> float:
    now = time.localtime()
    return time.mktime((now.tm_year, now.tm_mon, now.tm_mday, 0, 0, 0, 0, 0, -1))
