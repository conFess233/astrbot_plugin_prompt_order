"""片段、来源片和钩子改动记录。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

Zone = Literal["system", "tail", "tools"]

# 来源名约定：插件用插件名；宿主内置流程用 BUILTIN_OWNER；
# 在任何钩子之外发生的改动记为 EXTERNAL_OWNER。
BUILTIN_OWNER = "AstrBot"
EXTERNAL_OWNER = "钩子外改动"


@dataclass(slots=True)
class Run:
    """system 文本中归属同一来源的一段连续文字。"""

    owner: str
    text: str
    mark: str = ""


@dataclass(slots=True)
class Segment:
    """一次请求中可排序的最小单位。"""

    key: str
    owner: str
    zone: Zone
    text: str
    order: int
    temp: bool = False
    media: bool = False
    # 插件自己生成的片段（常驻说明），不来自请求。
    synthetic: bool = False
    # 原始 part / tool 对象，只在内存中使用，不序列化。
    ref: Any = field(default=None, repr=False)

    def to_dict(self, *, with_text: bool = True) -> dict[str, Any]:
        data: dict[str, Any] = {
            "key": self.key,
            "owner": self.owner,
            "zone": self.zone,
            "order": self.order,
            "temp": self.temp,
            "media": self.media,
            "synthetic": self.synthetic,
            "length": len(self.text),
        }
        if with_text:
            data["text"] = self.text
        return data


@dataclass(slots=True)
class HookChange:
    """某个钩子执行前后对请求造成的一项改动。"""

    owner: str
    handler: str
    kind: Literal["system", "tail", "tools", "prompt", "contexts"]
    summary: str
    after_arrange: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "owner": self.owner,
            "handler": self.handler,
            "kind": self.kind,
            "summary": self.summary,
            "after_arrange": self.after_arrange,
        }
