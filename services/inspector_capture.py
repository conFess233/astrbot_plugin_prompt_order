"""检查器的调用层与线路层截获。

- 调用层：包装每个 provider 实例的 text_chat / text_chat_stream，记录模型、轮次、耗时、用量与响应。
- 线路层：给 provider 底层的 httpx.AsyncClient 加事件钩子，记录真正发出的请求与每次重试；
  拿不到 httpx 客户端时退回包装 _query / _query_stream。
- 发送前：包装 _finally_convert_payload，按开关清除 DeepSeek 请求里已结束轮次的思考内容。
三层之间通过 ContextVar 关联，并发请求互不串号。
"""

from __future__ import annotations

import contextlib
import sys
import time
from contextvars import ContextVar
from typing import Any

import httpx
from astrbot.api import logger

from ..models.trace import Attempt, Round, Trace
from ..utils.redact import (
    decode_body,
    fold_media,
    keep_response_headers,
    redact_headers,
    to_jsonable,
)

CURRENT_TRACE: ContextVar[Trace | None] = ContextVar("po_trace", default=None)
CURRENT_ROUND: ContextVar[Round | None] = ContextVar("po_round", default=None)
TEXT_LIMIT = 4000
_OWN_DIR = __file__.rsplit("services", 1)[0]


def find_httpx_client(provider: Any) -> httpx.AsyncClient | None:
    client = getattr(provider, "client", None)
    api = getattr(client, "_api_client", None)
    for candidate in (
        client,
        getattr(client, "_client", None),
        getattr(api, "_async_httpx_client", None),
        getattr(api, "_httpx_client", None),
    ):
        if isinstance(candidate, httpx.AsyncClient):
            return candidate
    return None


def _frames(limit: int = 40):
    frame = sys._getframe(2)
    for _ in range(limit):
        if frame is None:
            return
        yield frame
        frame = frame.f_back


_SKIP_PARTS = ("/asyncio/", "/provider/", "/concurrent/")


def call_site() -> str:
    """调用位置：优先标出发起调用的插件，其次是第一个有意义的栈帧。

    跳过本插件、provider 实现、asyncio，以及其他插件套在 provider 上的包装函数。
    """

    own = _OWN_DIR.replace("\\", "/")
    first = ""
    for frame in _frames():
        path = frame.f_code.co_filename.replace("\\", "/")
        name = frame.f_code.co_name
        if path.startswith(own) or any(p in path for p in _SKIP_PARTS) or name == "wrapper":
            continue
        where = f"{path.rsplit('/', 1)[-1]}:{name}"
        first = first or where
        if "/plugins/" in path:
            plugin = path.split("/plugins/", 1)[1].split("/", 1)[0]
            return f"{plugin} · {where}"
    return first or "unknown"


def is_main_call(trace: Trace | None, kwargs: dict[str, Any]) -> bool:
    """主 Agent 的调用带着本轮请求的 session_id 与同一个 func_tool 对象。

    4.29 的 runner 在独立任务里调用 provider，调用栈上看不到 runner，不能靠栈判断；
    llm_generate 等旁路调用不传 session_id。
    """

    if trace is None or trace.status != "open" or not trace.session_id:
        return False
    if kwargs.get("session_id") != trace.session_id:
        return False
    func_tool = kwargs.get("func_tool")
    req = trace.req_ref
    return func_tool is None or req is None or func_tool is getattr(req, "func_tool", None)


def is_deepseek(provider: Any, model: str) -> bool:
    """模型名或接口地址里带 deepseek 时视为 DeepSeek。"""

    if "deepseek" in (model or "").lower():
        return True
    base_url = getattr(getattr(provider, "client", None), "base_url", None)
    return "deepseek" in str(getattr(base_url, "host", "") or "").lower()


def strip_history_reasoning(payloads: dict[str, Any]) -> int:
    """把已结束轮次里 assistant 消息的 reasoning_content 清空，返回清空的条数。

    最后一条不带工具调用的 assistant 是上一轮的最终回复，它和它之前的消息都属于已结束的轮次；
    之后是本轮的工具调用链，DeepSeek 要求原样回传思考内容，不动。
    字段保留为空字符串：DeepSeek V4 要求历史 assistant 消息带这个字段。
    只替换列表里的元素，不修改原消息字典，避免影响 AstrBot 保存的对话历史。
    """

    messages = payloads.get("messages")
    if not isinstance(messages, list):
        return 0
    last_final = -1
    for index, msg in enumerate(messages):
        if isinstance(msg, dict) and msg.get("role") == "assistant" and not msg.get("tool_calls"):
            last_final = index
    count = 0
    for index in range(last_final + 1):
        msg = messages[index]
        if not isinstance(msg, dict) or msg.get("role") != "assistant":
            continue
        if not msg.get("reasoning_content"):
            continue
        if msg.get("content") in (None, "", []) and not msg.get("tool_calls"):
            # 只有思考的消息清空后会被 AstrBot 当作空消息丢掉，保留原样。
            continue
        messages[index] = {**msg, "reasoning_content": ""}
        count += 1
    return count


def _is_own_capture(fn: Any) -> bool:
    """按代码对象识别本插件的 provider 包装，避免把复制了标记属性的外来包装当成自己的。"""

    code = getattr(fn, "__code__", None)
    return (
        code is not None
        and code.co_name == "wrapper"
        and code.co_filename.replace("\\", "/").endswith("/services/inspector_capture.py")
        and "astrbot_plugin_prompt_order" in code.co_filename
    )


def _wrapper_chain(fn: Any, limit: int = 30):
    """沿包装链向内遍历：本插件用 _po_inner，prompt_inspector 用 _pi_original。"""

    for _ in range(limit):
        if fn is None:
            return
        yield fn
        fn = (
            getattr(fn, "_po_inner", None)
            or getattr(fn, "_pi_original", None)
            or getattr(fn, "__wrapped__", None)
        )


class ProviderCapture:
    """管理 provider 包装与 httpx 钩子的安装、补装和卸载。"""

    def __init__(self, hub: Any) -> None:
        self.hub = hub
        self.active = True
        self.wire_modes: dict[str, str] = {}
        self._patched: list[tuple[Any, str, Any, bool, Any]] = []
        self._hooked: list[tuple[httpx.AsyncClient, Any, Any]] = []
        self._inflight: dict[int, tuple[Attempt, float]] = {}

    # ---------- 安装 ----------

    def refresh(self, providers: list[Any]) -> int:
        count = 0
        for provider in providers:
            try:
                count += self._patch_provider(provider)
            except Exception:
                logger.warning("提示词编排：包装 provider 失败，跳过该 provider", exc_info=True)
        return count

    def _patch_provider(self, provider: Any) -> int:
        count = 0
        for name, factory in (
            ("text_chat", self._chat_wrapper),
            ("text_chat_stream", self._stream_wrapper),
        ):
            count += self._patch(provider, name, factory)
        if hasattr(provider, "_finally_convert_payload"):
            count += self._patch(provider, "_finally_convert_payload", self._payload_wrapper)
        pid = _provider_id(provider)
        client = find_httpx_client(provider)
        if client is not None:
            self._hook_client(client)
            self.wire_modes[pid] = "httpx"
        elif hasattr(provider, "_query"):
            for name, factory in (
                ("_query", self._query_wrapper),
                ("_query_stream", self._query_stream_wrapper),
            ):
                if hasattr(provider, name):
                    count += self._patch(provider, name, factory)
            self.wire_modes[pid] = "_query"
        else:
            self.wire_modes[pid] = "none"
        return count

    def _patch(self, provider: Any, name: str, factory) -> int:
        current = provider.__dict__.get(name)
        # 其他插件可能在本插件的包装外面再包一层；只看最外层会反复叠加包装。
        if any(
            _is_own_capture(fn) and getattr(fn, "_po_capture", None) is self
            for fn in _wrapper_chain(current)
        ):
            return 0
        if _is_own_capture(current):
            # 最外层是本插件旧实例的包装：拆回它包住的原方法。
            original = current._po_inner
            had_attr = current._po_had_attr
            if had_attr:
                setattr(provider, name, original)
            else:
                delattr(provider, name)
        had_attr = name in provider.__dict__
        original = getattr(provider, name, None)
        if original is None:
            return 0
        wrapper = factory(provider, original)
        wrapper._po_capture = self
        wrapper._po_inner = original
        wrapper._po_had_attr = had_attr
        setattr(provider, name, wrapper)
        self._patched.append((provider, name, wrapper, had_attr, original))
        return 1

    def _hook_client(self, client: httpx.AsyncClient) -> None:
        hooks = client.event_hooks
        requests = hooks.get("request", [])
        if any(getattr(fn, "__self__", None) is self for fn in requests) and not any(
            _is_stale_hook(fn, self) for fn in requests
        ):
            # 每次请求都会补装一遍；已经装好时直接返回，不重建列表。
            return
        # 去掉本插件旧实例留下的钩子（插件重载后类对象不同，只能按名字识别）。
        for kind in ("request", "response"):
            hooks[kind] = [fn for fn in hooks.get(kind, []) if not _is_stale_hook(fn, self)]
        if any(getattr(fn, "__self__", None) is self for fn in hooks.get("request", [])):
            return
        on_request, on_response = self._on_request, self._on_response
        hooks.setdefault("request", []).append(on_request)
        hooks.setdefault("response", []).append(on_response)
        client.event_hooks = hooks
        self._hooked.append((client, on_request, on_response))

    def uninstall(self) -> None:
        self.active = False
        for provider, name, wrapper, had_attr, original in self._patched:
            if provider.__dict__.get(name) is wrapper:
                if had_attr:
                    setattr(provider, name, original)
                else:
                    delattr(provider, name)
        self._patched.clear()
        for client, on_request, on_response in self._hooked:
            try:
                hooks = client.event_hooks
                hooks["request"] = [fn for fn in hooks.get("request", []) if fn is not on_request]
                hooks["response"] = [
                    fn for fn in hooks.get("response", []) if fn is not on_response
                ]
                client.event_hooks = hooks
            except Exception:
                logger.warning("提示词编排：移除 httpx 钩子失败", exc_info=True)
        self._hooked.clear()
        self._inflight.clear()

    # ---------- 轮次 ----------

    def _begin(self, provider: Any, kind: str, kwargs: dict[str, Any]):
        if not self.active or not self.hub.capturing():
            return None
        running = CURRENT_ROUND.get()
        if running is not None and running.duration_ms is None:
            # 同一次调用经过了多层本插件的包装（例如旧实例遗留），只由最外层记录。
            return None
        trace = CURRENT_TRACE.get()
        if not is_main_call(trace, kwargs):
            trace = self.hub.open_side(call_site())
            if trace is None:
                return None
        pid = _provider_id(provider)
        config = getattr(provider, "provider_config", {}) or {}
        model = kwargs.get("model") or _safe(provider.get_model) or ""
        func_tool = kwargs.get("func_tool")
        tools = getattr(func_tool, "tools", None) or []
        contexts = kwargs.get("contexts") or []
        rnd = Round(
            index=len(trace.rounds) + 1,
            kind=kind,
            call_site=call_site(),
            provider_id=pid,
            provider_type=str(config.get("type", "")),
            model=str(model),
            started_at=time.time(),
            wire=self.wire_modes.get(pid, "none")
            if self.hub.settings.inspector.depth == "wire"
            else "call",
            params={
                "session_id": kwargs.get("session_id"),
                "tool_choice": kwargs.get("tool_choice"),
                "contexts": len(contexts) if isinstance(contexts, list) else 0,
                "tools": [str(getattr(t, "name", "")) for t in tools],
                "system_len": len(kwargs.get("system_prompt") or ""),
            },
        )
        if rnd.wire in ("call", "none"):
            # 没有线路层时保存调用层入参，页面标注“非线路层”。
            rnd.params["input"] = fold_media(
                to_jsonable(
                    {
                        "system_prompt": kwargs.get("system_prompt"),
                        "prompt": kwargs.get("prompt"),
                        "contexts": contexts,
                        "extra_user_content_parts": kwargs.get("extra_user_content_parts"),
                    }
                )
            )
        trace.rounds.append(rnd)
        trace.touched_at = time.time()
        token = CURRENT_ROUND.set(rnd)
        self.hub.round_started(trace, rnd)
        return trace, rnd, token

    def _end(self, trace: Trace, rnd: Round, started: float, error: str, resp: Any) -> None:
        try:
            rnd.duration_ms = round((time.perf_counter() - started) * 1000, 1)
            rnd.error = error
            if resp is not None:
                rnd.usage = _usage(resp)
                rnd.response = _response(resp, self.hub.settings.inspector.save_response)
            for attempt in rnd.attempts:
                if attempt.status is None and not attempt.error:
                    attempt.error = "未收到响应"
            for key, (attempt, _) in list(self._inflight.items()):
                if attempt in rnd.attempts:
                    self._inflight.pop(key, None)
            trace.touched_at = time.time()
            self.hub.round_finished(trace, rnd)
        except Exception:
            logger.warning("提示词编排：记录轮次结果失败", exc_info=True)

    # ---------- 包装函数 ----------

    def _chat_wrapper(self, provider: Any, original: Any):
        capture = self

        async def wrapper(*args, **kwargs):
            ctx = capture._begin(provider, "chat", kwargs)
            if ctx is None:
                return await original(*args, **kwargs)
            trace, rnd, token = ctx
            started = time.perf_counter()
            error, resp = "", None
            try:
                resp = await original(*args, **kwargs)
                return resp
            except BaseException as exc:
                error = f"{type(exc).__name__}: {exc}"[:500]
                raise
            finally:
                CURRENT_ROUND.reset(token)
                capture._end(trace, rnd, started, error, resp)

        return wrapper

    def _stream_wrapper(self, provider: Any, original: Any):
        capture = self

        async def wrapper(*args, **kwargs):
            ctx = capture._begin(provider, "stream", kwargs)
            if ctx is None:
                async for item in original(*args, **kwargs):
                    yield item
                return
            trace, rnd, token = ctx
            started = time.perf_counter()
            error, final, usage_chunk = "", None, None
            try:
                async for item in original(*args, **kwargs):
                    if rnd.ttft_ms is None:
                        rnd.ttft_ms = round((time.perf_counter() - started) * 1000, 1)
                    if getattr(item, "usage", None) is not None:
                        usage_chunk = item
                    if not getattr(item, "is_chunk", True):
                        final = item
                    yield item
            except BaseException as exc:
                error = f"{type(exc).__name__}: {exc}"[:500]
                raise
            finally:
                # 生成器在其他上下文中被关闭时无法还原，丢弃即可。
                with contextlib.suppress(ValueError):
                    CURRENT_ROUND.reset(token)
                resp = final or usage_chunk
                if (
                    final is not None
                    and getattr(final, "usage", None) is None
                    and usage_chunk is not None
                ):
                    final.usage = usage_chunk.usage
                capture._end(trace, rnd, started, error, resp)

        return wrapper

    def _payload_wrapper(self, provider: Any, original: Any):
        capture = self

        def wrapper(payloads, *args, **kwargs):
            result = original(payloads, *args, **kwargs)
            if not capture.active or not capture.hub.settings.strip_reasoning:
                return result
            try:
                if isinstance(payloads, dict) and is_deepseek(
                    provider, str(payloads.get("model", ""))
                ):
                    count = strip_history_reasoning(payloads)
                    rnd = CURRENT_ROUND.get()
                    if rnd is not None and rnd.duration_ms is None:
                        rnd.params["reasoning_stripped"] = count
            except Exception:
                logger.warning("提示词编排：清除历史思考失败，本次请求保持原样", exc_info=True)
            return result

        return wrapper

    def _query_wrapper(self, provider: Any, original: Any):
        capture = self

        async def wrapper(payloads, *args, **kwargs):
            capture._record_query(payloads)
            return await original(payloads, *args, **kwargs)

        return wrapper

    def _query_stream_wrapper(self, provider: Any, original: Any):
        capture = self

        async def wrapper(payloads, *args, **kwargs):
            capture._record_query(payloads)
            async for item in original(payloads, *args, **kwargs):
                yield item

        return wrapper

    def _record_query(self, payloads: Any) -> None:
        rnd = CURRENT_ROUND.get()
        if rnd is None or self.hub.settings.inspector.depth != "wire":
            return
        try:
            body = to_jsonable(dict(payloads))
            if self.hub.settings.inspector.fold_media:
                body = fold_media(body)
            rnd.attempts.append(
                Attempt(
                    method="CALL", url="provider._query", started_at=time.time(), request_body=body
                )
            )
        except Exception:
            logger.warning("提示词编排：记录 _query 入参失败", exc_info=True)

    # ---------- httpx 钩子 ----------

    async def _on_request(self, request: httpx.Request) -> None:
        rnd = CURRENT_ROUND.get()
        if rnd is None or not self.active or self.hub.settings.inspector.depth != "wire":
            return
        try:
            try:
                raw = request.content
            except httpx.RequestNotRead:
                raw = None
            settings = self.hub.settings.inspector
            body, truncated = decode_body(
                raw, fold=settings.fold_media, limit=settings.body_limit_kb * 1024
            )
            attempt = Attempt(
                method=request.method,
                url=str(request.url),
                started_at=time.time(),
                request_headers=redact_headers(request.headers),
                request_body=body,
                body_truncated=truncated,
            )
            rnd.attempts.append(attempt)
            self._inflight[id(request)] = (attempt, time.perf_counter())
            self.hub.attempt_event(rnd, attempt)
        except Exception:
            logger.warning("提示词编排：记录 HTTP 请求失败", exc_info=True)

    async def _on_response(self, response: httpx.Response) -> None:
        item = self._inflight.pop(id(response.request), None)
        if item is None:
            return
        attempt, started = item
        attempt.status = response.status_code
        attempt.duration_ms = round((time.perf_counter() - started) * 1000, 1)
        attempt.response_headers = keep_response_headers(response.headers)


def _is_stale_hook(fn: Any, current: ProviderCapture) -> bool:
    owner = getattr(fn, "__self__", None)
    return (
        owner is not None
        and owner is not current
        and type(owner).__name__ == "ProviderCapture"
        and getattr(fn, "__name__", "") in ("_on_request", "_on_response")
    )


def _provider_id(provider: Any) -> str:
    config = getattr(provider, "provider_config", {}) or {}
    return str(config.get("id", "") or type(provider).__name__)


def _safe(fn: Any) -> Any:
    try:
        return fn()
    except Exception:
        return None


def _usage(resp: Any) -> dict[str, Any]:
    usage = getattr(resp, "usage", None)
    data: dict[str, Any] = {}
    if usage is not None:
        for key in ("input", "input_cached", "input_other", "output", "total"):
            value = getattr(usage, key, None)
            if value is not None:
                data[key] = int(value)
    raw = getattr(getattr(resp, "raw_completion", None), "usage", None)
    if raw is not None:
        raw_data = to_jsonable(raw)
        data["raw"] = raw_data
        details = raw_data.get("completion_tokens_details") if isinstance(raw_data, dict) else None
        if isinstance(details, dict) and details.get("reasoning_tokens") is not None:
            data["reasoning"] = details["reasoning_tokens"]
    return data


def _response(resp: Any, save_text: bool) -> dict[str, Any]:
    data: dict[str, Any] = {"id": getattr(resp, "id", None)}
    raw = getattr(resp, "raw_completion", None)
    choices = getattr(raw, "choices", None)
    if choices:
        data["finish_reason"] = getattr(choices[0], "finish_reason", None)
    names = list(getattr(resp, "tools_call_name", None) or [])
    args = list(getattr(resp, "tools_call_args", None) or [])
    data["tool_calls"] = [
        {"name": name, "args": str(to_jsonable(args[i]) if i < len(args) else "")[:1000]}
        for i, name in enumerate(names)
    ]
    if save_text:
        try:
            data["text"] = (resp.completion_text or "")[:TEXT_LIMIT]
        except Exception:
            data["text"] = ""
        reasoning = getattr(resp, "reasoning_content", None) or ""
        data["reasoning"] = reasoning[:TEXT_LIMIT]
    return data
