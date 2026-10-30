"""给其他插件的 on_llm_request 钩子套一层，执行前后各拍一次快照。

不修改 ProviderRequest 类，只替换 StarHandlerMetadata.handler；
AstrBot 在调用每个钩子时才读取这个属性，所以本轮内补上的包装立即生效。
"""

from __future__ import annotations

import functools
from collections.abc import Callable
from typing import Any

from astrbot.api import logger
from astrbot.core.provider.entities import ProviderRequest
from astrbot.core.star.star import star_map
from astrbot.core.star.star_handler import EventType, star_handlers_registry

MARK = "_po_original"


def owner_of_module(module_path: str | None) -> str:
    """模块路径 → 插件名；宿主内置模块返回空字符串。"""

    if not module_path:
        return ""
    meta = star_map.get(module_path)
    name = getattr(meta, "name", "") if meta else ""
    if name:
        return name
    parts = module_path.split(".")
    if "plugins" in parts:
        idx = parts.index("plugins")
        if idx + 1 < len(parts):
            return parts[idx + 1]
    return "" if module_path.startswith("astrbot.") else module_path


def _is_own_wrapper(fn: Any) -> bool:
    """按代码对象识别本插件生成的包装函数；标记属性可能被别人的 functools.wraps 复制。"""

    code = getattr(fn, "__code__", None)
    return (
        code is not None
        and code.co_filename.replace("\\", "/").endswith("/services/hook_wrapper.py")
        and code.co_name == "wrapper"
        and "astrbot_plugin_prompt_order" in code.co_filename
    )


def _wrapped_chain(fn: Any, limit: int = 30):
    for _ in range(limit):
        if fn is None:
            return
        yield fn
        fn = getattr(fn, "__wrapped__", None)


class HookWrapper:
    """管理包装的安装、补装与卸载。"""

    def __init__(self, own_module: str, on_before: Callable, on_after: Callable) -> None:
        self._own_module = own_module
        self._on_before = on_before
        self._on_after = on_after
        # 卸载后包装切换成直接透传；外层又被别人包住时不能硬拆。
        self.active = True
        self._wrapped: list[tuple[Any, Callable]] = []

    def owner_of(self, module_path: str) -> str:
        return owner_of_module(module_path) or module_path

    def ensure(self) -> int:
        """给尚未包装的 on_llm_request 钩子补上包装，返回本次新包装的数量。"""

        count = 0
        try:
            handlers = star_handlers_registry.get_handlers_by_event_type(
                EventType.OnLLMRequestEvent, only_activated=False
            )
        except Exception:
            logger.warning("提示词编排：读取钩子列表失败，本轮不追踪来源", exc_info=True)
            return 0
        live = {id(meta) for meta in handlers}
        # 插件重载后旧的元数据对象不再出现在注册表里，不再持有它们。
        self._wrapped = [(m, w) for m, w in self._wrapped if id(m) in live]
        for meta in handlers:
            if meta.handler_module_path.startswith(self._own_module):
                continue
            current = meta.handler
            # 其他插件（如 AstrNa）可能用 functools.wraps 把本插件的包装再包一层，
            # 它会复制本插件的标记属性，所以必须沿 __wrapped__ 找到真正的包装再判断。
            if any(
                _is_own_wrapper(fn) and getattr(fn, "_po_owner_tag", None) is self
                for fn in _wrapped_chain(current)
            ):
                continue
            if _is_own_wrapper(current):
                # 最外层是本插件旧实例的包装：拆回原函数再包。被别人包在里面的旧包装保持透传。
                current = getattr(current, MARK)
            meta.handler = self._make(meta, current)
            self._wrapped.append((meta, meta.handler))
            count += 1
        return count

    def _make(self, meta: Any, original: Callable) -> Callable:
        owner = self.owner_of(meta.handler_module_path)
        handler_name = meta.handler_name
        hub = self

        @functools.wraps(original)
        async def wrapper(event, *args, **kwargs):
            req = args[0] if args and isinstance(args[0], ProviderRequest) else None
            token = None
            if hub.active and req is not None:
                try:
                    token = hub._on_before(event, req)
                except Exception:
                    logger.warning("提示词编排：钩子前快照失败", exc_info=True)
            try:
                return await original(event, *args, **kwargs)
            finally:
                if token is not None:
                    try:
                        hub._on_after(event, req, token, owner, handler_name)
                    except Exception:
                        logger.warning("提示词编排：钩子后对比失败", exc_info=True)

        setattr(wrapper, MARK, original)
        wrapper._po_owner_tag = self
        return wrapper

    def uninstall(self) -> None:
        """只还原仍是本插件包装的钩子；还原不了的保持透传。"""

        self.active = False
        restored = 0
        for meta, wrapper in self._wrapped:
            if meta.handler is wrapper:
                meta.handler = getattr(wrapper, MARK)
                restored += 1
        self._wrapped.clear()
        logger.info(f"提示词编排：已还原 {restored} 个钩子包装")
