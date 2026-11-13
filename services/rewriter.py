"""本轮片段的改写：用户改写规则、JSON 压缩与常驻说明片段。

只做确定性的文本变换；规则出错时保留原文并记录警告。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from typing import Any

from astrbot.api import logger

from ..models.layout import Layout, Rewrite
from ..models.segment import Segment
from ..utils.jsontext import minify_json

NOTE_OWNER = "prompt_order"


def target_matches(target: str, key: str, owner: str) -> bool:
    """规则目标是否命中片段：精确 key（忽略 #序号）、来源/*，或含通配符的 key。"""

    base = key.split("#", 1)[0]
    if target in (key, base):
        return True
    if target.endswith("/*") and target[:-2] == owner:
        return True
    return any(ch in target for ch in "*?[") and (
        fnmatchcase(key, target) or fnmatchcase(base, target)
    )


def apply_rule(rule: Rewrite, text: str) -> tuple[str, int]:
    """返回 (新文本, 命中次数)。

    保存布局时已校验正则本身；替换模板里的分组引用（如 \\3）只有匹配时才会出错，
    这时保留原文并记录警告。
    """

    if rule.regex:
        try:
            return re.subn(rule.find, rule.replace, text)
        except re.error as exc:
            logger.warning(
                f"提示词编排：改写规则执行失败，已保留原文，目标={rule.target}，原因={exc}"
            )
            return text, 0
    count = text.count(rule.find)
    return (text.replace(rule.find, rule.replace), count) if count else (text, 0)


@dataclass
class RuleHit:
    index: int
    target: str
    hits: int = 0
    saved: int = 0


@dataclass
class RewriteReport:
    json_blocks: int = 0
    json_saved: int = 0
    rules: list[RuleHit] = field(default_factory=list)

    @property
    def rule_saved(self) -> int:
        return sum(r.saved for r in self.rules)

    @property
    def changed(self) -> bool:
        return bool(self.json_blocks or any(r.hits for r in self.rules))

    def summary(self) -> str:
        bits = []
        if self.json_blocks:
            bits.append(f"JSON 压缩 {self.json_blocks} 块，少 {self.json_saved} 字")
        hits = sum(r.hits for r in self.rules)
        if hits:
            bits.append(f"改写规则命中 {hits} 处，少 {self.rule_saved} 字")
        return "；".join(bits)

    def to_dict(self) -> dict[str, Any]:
        return {
            "json_blocks": self.json_blocks,
            "json_saved": self.json_saved,
            "rule_saved": self.rule_saved,
            "rules": [
                {"index": r.index, "target": r.target, "hits": r.hits, "saved": r.saved}
                for r in self.rules
            ],
        }


def rewrite_text(
    text: str,
    *,
    key: str,
    owner: str,
    temp: bool,
    rules: list[Rewrite],
    minify: bool,
    report: RewriteReport,
) -> str:
    """先套用户规则，再压缩 JSON（只对不存档的片段）。"""

    for i, rule in enumerate(rules):
        if not rule.enabled or not target_matches(rule.target, key, owner):
            continue
        new, hits = apply_rule(rule, text)
        if hits:
            hit = next((r for r in report.rules if r.index == i), None)
            if hit is None:
                hit = RuleHit(i, rule.target)
                report.rules.append(hit)
            hit.hits += hits
            hit.saved += len(text) - len(new)
            text = new
    if minify and temp:
        new = minify_json(text)
        if new != text:
            report.json_blocks += 1
            report.json_saved += len(text) - len(new)
            text = new
    return text


def note_segments(layout: Layout, start: int) -> list[Segment]:
    """布局里的常驻说明作为 SYSTEM 区片段加入本轮。"""

    return [
        Segment(
            key=note.key,
            owner=NOTE_OWNER,
            zone="system",
            text=note.text,
            order=start + i,
            synthetic=True,
        )
        for i, note in enumerate(layout.notes)
    ]
