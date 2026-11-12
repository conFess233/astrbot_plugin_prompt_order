"""按布局重排片段：分配区与位置、校验硬约束、组装并检查内容守恒。"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from typing import Any

from ..models.layout import MEDIA, UNPLACED, Layout
from ..models.segment import Segment


@dataclass
class ArrangeResult:
    """重排结果；对应字段为 None 表示该区无需改动。"""

    system: str | None = None
    parts: list[Any] | None = None
    tools: list[Any] | None = None
    violations: list[str] = field(default_factory=list)
    plan: dict[str, list[str]] = field(default_factory=dict)
    system_segments: list[Segment] = field(default_factory=list)
    parts_owners: list[str] = field(default_factory=list)
    error: str = ""

    @property
    def changed(self) -> bool:
        return self.system is not None or self.parts is not None or self.tools is not None


def pattern_rank(
    key: str, owner: str, entries: list[str], seg_zone: str = "", zone: str = ""
) -> int | None:
    """片段在某个区的排位：精确 key 优先，其次 `来源/*`。

    通配符只匹配原本就在该区的片段；跨区移动必须写出具体片段，避免整组误移。
    """

    base = key.split("#", 1)[0]
    for idx, entry in enumerate(entries):
        if entry in (key, base):
            return idx
    if seg_zone and zone and seg_zone != zone:
        return None
    for idx, entry in enumerate(entries):
        if entry.endswith("/*") and entry[:-2] == owner:
            return idx
    return None


def _fallback_rank(entries: list[str], marker: str) -> int:
    return entries.index(marker) if marker in entries else len(entries)


def arrange(
    segments: list[Segment],
    layout: Layout,
    *,
    original_system: str,
    make_temp_part,
    make_toolset,
) -> ArrangeResult:
    """计算新顺序；任何不满足约束的片段退回原区。"""

    result = ArrangeResult()
    text_segs = [s for s in segments if s.zone != "tools"]
    placed: dict[str, list[tuple[int, int, Segment]]] = {"system": [], "tail": []}
    zones = {"system": layout.system, "tail": layout.tail}
    for seg in text_segs:
        target, rank = None, None
        if seg.media:
            target, rank = "tail", _fallback_rank(layout.tail, MEDIA)
        elif seg.synthetic:
            # 常驻说明只放在 SYSTEM 区；没排进布局时落在“未排片段”处。
            target = "system"
            rank = pattern_rank(seg.key, seg.owner, layout.system)
            if rank is None:
                rank = _fallback_rank(layout.system, UNPLACED)
        else:
            for zone in ("system", "tail"):
                rank = pattern_rank(seg.key, seg.owner, zones[zone], seg.zone, zone)
                if rank is not None:
                    target = zone
                    break
        if target == "system" and seg.zone == "tail" and not seg.temp:
            result.violations.append(f"C1：{seg.key} 会写入历史，不能移进 system")
            target = None
        if target is None:
            target = seg.zone
            rank = _fallback_rank(zones[target], UNPLACED)
        placed[target].append((rank, seg.order, seg))

    for zone in placed:
        placed[zone].sort(key=lambda item: (item[0], item[1]))
    new_system = [seg for _, _, seg in placed["system"]]
    new_tail = [seg for _, _, seg in placed["tail"]]
    old_system = [s for s in text_segs if s.zone == "system"]
    old_tail = [s for s in text_segs if s.zone == "tail"]
    result.plan = {
        "system": [s.key for s in new_system],
        "tail": [s.key for s in new_tail],
    }

    # 常驻说明不在原 system 文本里，有它时必须重建 system。
    has_notes = any(s.synthetic for s in new_system)
    if has_notes or [id(s) for s in new_system] != [id(s) for s in old_system]:
        result.system_segments = [s for s in new_system if s.text.strip()]
        result.system = layout.separator.join(s.text.strip() for s in result.system_segments)
        if result.system == original_system:
            result.system = None
    if [id(s) for s in new_tail] != [id(s) for s in old_tail]:
        parts: list[Any] = []
        for seg in new_tail:
            parts.append(seg.ref if seg.zone == "tail" else make_temp_part(seg.text.strip()))
            result.parts_owners.append(seg.owner)
        result.parts = parts

    before = Counter(s.text.strip() for s in text_segs if s.text.strip() and not s.media)
    after = Counter(s.text.strip() for s in new_system + new_tail if s.text.strip() and not s.media)
    if before != after:
        result.error = "C4：重排前后内容不一致，已放弃本次重排"
        result.system = result.parts = None
        return result

    tools = [s for s in segments if s.zone == "tools"]
    ordered = _order_tools(tools, layout)
    if [id(s) for s in ordered] != [id(s) for s in tools]:
        result.tools = make_toolset([s.ref for s in ordered])
    result.plan["tools"] = [s.text for s in ordered]
    return result


def _order_tools(tools: list[Segment], layout: Layout) -> list[Segment]:
    if layout.tools_mode == "by_name":
        return sorted(tools, key=lambda s: s.text)
    if layout.tools_mode == "custom":
        index = {name: idx for idx, name in enumerate(layout.tools_order)}
        rest = len(index)
        return sorted(tools, key=lambda s: (index.get(s.text, rest), s.text))
    return list(tools)


def match_layout(layouts: list[Layout], umo: str) -> Layout | None:
    """按顺序找第一个范围命中的布局；范围为空的布局作为兜底。"""

    fallback = None
    for layout in layouts:
        if not layout.umo_globs:
            fallback = fallback or layout
            continue
        if any(fnmatchcase(umo, pattern) for pattern in layout.umo_globs):
            return layout
    return fallback
