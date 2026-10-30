"""system 文本的来源表维护：把一次改写的差异映射到每段文字的来源。"""

from __future__ import annotations

from difflib import SequenceMatcher

from ..models.segment import Run

# 单个替换块超过这个长度就不再逐字对比，整块归给本次改写者。
CHAR_DIFF_LIMIT = 4000


def runs_text(runs: list[Run]) -> str:
    return "".join(run.text for run in runs)


def compact(runs: list[Run]) -> list[Run]:
    """合并相邻且来源、标记都相同的片，去掉空片。"""

    merged: list[Run] = []
    for run in runs:
        if not run.text:
            continue
        if merged and merged[-1].owner == run.owner and merged[-1].mark == run.mark:
            merged[-1] = Run(run.owner, merged[-1].text + run.text, run.mark)
        else:
            merged.append(Run(run.owner, run.text, run.mark))
    return merged


def apply_change(
    runs: list[Run], new: str, owner: str, *, max_chars: int = 50000
) -> tuple[list[Run], dict[str, int], str]:
    """按新文本更新来源表。

    Returns:
        (新来源表, 被删改的其他来源 -> 删改字数, 走的路径 fast/diff/whole)
    """

    old = runs_text(runs)
    if new == old:
        return runs, {}, "fast"
    if new.startswith(old):
        return compact([*runs, Run(owner, new[len(old) :])]), {}, "fast"
    if new.endswith(old):
        return compact([Run(owner, new[: len(new) - len(old)]), *runs]), {}, "fast"
    if len(old) > max_chars or len(new) > max_chars:
        touched = _owner_sizes(runs)
        touched.pop(owner, None)
        return [Run(owner, new)], touched, "whole"

    # 逐字的来源数组；先按行对比，再对改动行逐字对比，避免长文本退化成平方级。
    owners = [(run.owner, run.mark) for run in runs for _ in run.text]
    new_owners: list[tuple[str, str]] = []
    touched: dict[str, int] = {}
    old_lines = old.splitlines(keepends=True)
    new_lines = new.splitlines(keepends=True)
    old_pos = _line_offsets(old_lines)
    new_pos = _line_offsets(new_lines)
    matcher = SequenceMatcher(None, old_lines, new_lines, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        a0, a1 = old_pos[i1], old_pos[i2]
        b0, b1 = new_pos[j1], new_pos[j2]
        if tag == "equal":
            new_owners.extend(owners[a0:a1])
            continue
        if tag == "insert":
            new_owners.extend([(owner, "")] * (b1 - b0))
            continue
        if tag == "delete":
            _count(touched, owners[a0:a1], owner)
            continue
        old_block, new_block = old[a0:a1], new[b0:b1]
        if len(old_block) + len(new_block) > CHAR_DIFF_LIMIT:
            _count(touched, owners[a0:a1], owner)
            new_owners.extend([(owner, "")] * len(new_block))
            continue
        chars = SequenceMatcher(None, old_block, new_block, autojunk=False)
        for ctag, c1, c2, d1, d2 in chars.get_opcodes():
            if ctag == "equal":
                new_owners.extend(owners[a0 + c1 : a0 + c2])
            else:
                _count(touched, owners[a0 + c1 : a0 + c2], owner)
                new_owners.extend([(owner, "")] * (d2 - d1))

    rebuilt: list[Run] = []
    start = 0
    for idx in range(1, len(new) + 1):
        if idx == len(new) or new_owners[idx] != new_owners[start]:
            who, mark = new_owners[start]
            rebuilt.append(Run(who, new[start:idx], mark))
            start = idx
    return compact(rebuilt), touched, "diff"


def _line_offsets(lines: list[str]) -> list[int]:
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line))
    return offsets


def _count(touched: dict[str, int], owners: list[tuple[str, str]], actor: str) -> None:
    for who, _ in owners:
        if who != actor:
            touched[who] = touched.get(who, 0) + 1


def _owner_sizes(runs: list[Run]) -> dict[str, int]:
    sizes: dict[str, int] = {}
    for run in runs:
        sizes[run.owner] = sizes.get(run.owner, 0) + len(run.text)
    return sizes


def split_by_anchors(text: str, anchors: list[tuple[str, bool]]) -> list[str]:
    """按锚点把一段文本切开；锚点所在位置成为新段开头。"""

    cuts: set[int] = set()
    for anchor, line_start in anchors:
        start = 0
        while anchor:
            idx = text.find(anchor, start)
            if idx < 0:
                break
            if idx > 0 and (not line_start or text[idx - 1] == "\n"):
                cuts.add(idx)
            start = idx + len(anchor)
    if not cuts:
        return [text]
    points = [0, *sorted(cuts), len(text)]
    return [text[a:b] for a, b in zip(points, points[1:], strict=False) if text[a:b]]
