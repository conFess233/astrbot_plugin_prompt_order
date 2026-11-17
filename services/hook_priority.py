"""on_llm_request 钩子的执行顺序：列出各插件的优先级，并按配置调整本插件重排钩子的位置。

AstrBot 只在注册钩子时按优先级排序，没有公开的修改接口。调整优先级时改本插件自己的
元数据，再按宿主相同的规则（优先级从高到低，相同则保持原顺序）重排宿主的钩子列表。
"""

from __future__ import annotations

from typing import Any

from astrbot.api import logger
from astrbot.core.star.star import star_map
from astrbot.core.star.star_handler import EventType, star_handlers_registry

from .hook_wrapper import owner_of_module

ARRANGE_HANDLER = "po_arrange"


def _priority(meta: Any) -> int:
    return int(meta.extras_configs.get("priority", 0))


def _hooks() -> list[Any]:
    return star_handlers_registry.get_handlers_by_event_type(
        EventType.OnLLMRequestEvent, only_activated=False
    )


def _own_arrange(handlers: list[Any], own_module: str) -> Any | None:
    for meta in handlers:
        if meta.handler_module_path.startswith(own_module) and meta.handler_name == ARRANGE_HANDLER:
            return meta
    return None


def hook_order(own_module: str) -> dict[str, Any]:
    """按实际执行顺序列出全部 on_llm_request 钩子，标出排在重排之后的。"""

    handlers = _hooks()
    arrange = _own_arrange(handlers, own_module)
    arrange_priority = _priority(arrange) if arrange is not None else None
    passed = False
    items = []
    for meta in handlers:
        own = meta.handler_module_path.startswith(own_module)
        if meta is arrange:
            passed = True
        star = star_map.get(meta.handler_module_path)
        items.append(
            {
                "plugin": owner_of_module(meta.handler_module_path) or "AstrBot",
                "handler": meta.handler_name,
                "priority": _priority(meta),
                "own": own,
                "activated": bool(getattr(star, "activated", True)),
                # 优先级相同时按加载顺序执行，插件重载后可能变。
                "after_arrange": passed and meta is not arrange and not own,
                "tie": arrange is not None
                and meta is not arrange
                and _priority(meta) == arrange_priority,
            }
        )
    return {"arrange_priority": arrange_priority, "hooks": items}


def set_arrange_priority(own_module: str, priority: int) -> bool:
    """把重排钩子调到指定优先级并重排宿主钩子列表；宿主结构不符时放弃并记录警告。"""

    arrange = _own_arrange(_hooks(), own_module)
    if arrange is None:
        logger.warning("提示词编排：没有找到重排钩子，优先级保持不变")
        return False
    if _priority(arrange) == priority:
        return True
    handlers = getattr(star_handlers_registry, "_handlers", None)
    if not isinstance(handlers, list):
        logger.warning("提示词编排：宿主钩子列表结构已变化，不能调整重排优先级，保持默认值")
        return False
    old = _priority(arrange)
    arrange.extras_configs["priority"] = priority
    handlers.sort(key=lambda h: -h.extras_configs.get("priority", 0))
    logger.info(f"提示词编排：重排钩子优先级 {old} → {priority}")
    return True
