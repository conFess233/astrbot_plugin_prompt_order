"""片段稳定度统计、最近样本、缓存警告与推荐排序。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any

from astrbot.api import logger

from ..models.layout import MEDIA, UNPLACED, Layout
from ..models.segment import Segment
from .arranger import pattern_rank
from .storage import atomic_write_json

STATIC, OCCASIONAL, DYNAMIC = "static", "occasional", "dynamic"


def text_hash(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8", "ignore")).hexdigest()[:12]


class StatsService:
    """按 (布局, 片段) 统计最近 N 次请求；样本保存在内存并定期落盘。"""

    def __init__(self, data_dir: Path, window: int, sample_limit: int) -> None:
        self.window = window
        self.db_path = data_dir / "stats.sqlite"
        self.samples_path = data_dir / "samples.json"
        self.samples: deque[dict[str, Any]] = deque(maxlen=sample_limit)
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None
        self._samples_dirty = False

    # ---------- 生命周期 ----------

    def open(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS obs ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, layout_id TEXT NOT NULL, "
            "ts REAL NOT NULL, payload TEXT NOT NULL)"
        )
        conn.execute("CREATE INDEX IF NOT EXISTS obs_layout ON obs(layout_id, id)")
        conn.commit()
        self._conn = conn
        if self.samples_path.exists():
            try:
                for item in json.loads(self.samples_path.read_text(encoding="utf-8")):
                    self.samples.append(item)
            except (OSError, ValueError):
                logger.warning("提示词编排：样本文件读取失败，从空样本开始", exc_info=True)

    def close(self) -> None:
        self.flush_samples()
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    # ---------- 写入 ----------

    def observe(self, layout_id: str, segments: list[Segment]) -> None:
        payload = [
            {
                "key": s.key,
                "owner": s.owner,
                "zone": s.zone,
                "hash": text_hash(s.text),
                "len": len(s.text),
                "temp": s.temp,
                "media": s.media,
                "order": s.order,
            }
            for s in segments
        ]
        with self._lock:
            if self._conn is None:
                return
            self._conn.execute(
                "INSERT INTO obs(layout_id, ts, payload) VALUES (?, ?, ?)",
                (layout_id, time.time(), json.dumps(payload, ensure_ascii=False)),
            )
            self._conn.execute(
                "DELETE FROM obs WHERE layout_id = ? AND id NOT IN "
                "(SELECT id FROM obs WHERE layout_id = ? ORDER BY id DESC LIMIT ?)",
                (layout_id, layout_id, self.window),
            )
            self._conn.commit()

    def add_sample(self, sample: dict[str, Any]) -> None:
        with self._lock:
            self.samples.append(sample)
            self._samples_dirty = True

    def update_sample_changes(self, state_id: int, changes: list[dict[str, Any]]) -> None:
        """补写某次请求重排之后的改动，并标记样本待写盘。"""

        with self._lock:
            for sample in reversed(self.samples):
                if sample.get("state_id") == state_id:
                    sample["changes"] = changes
                    self._samples_dirty = True
                    return

    def flush_samples(self) -> None:
        with self._lock:
            if not self._samples_dirty:
                return
            data = list(self.samples)
            self._samples_dirty = False
        try:
            atomic_write_json(self.samples_path, data)
        except OSError:
            logger.warning("提示词编排：样本写入失败，下次重试", exc_info=True)
            self._samples_dirty = True

    # ---------- 读取 ----------

    def segment_stats(self, layout_id: str) -> dict[str, Any]:
        """返回 {key: 统计}，以及窗口内的请求数。"""

        with self._lock:
            if self._conn is None:
                return {"requests": 0, "segments": {}}
            rows = self._conn.execute(
                "SELECT payload FROM obs WHERE layout_id = ? ORDER BY id DESC LIMIT ?",
                (layout_id, self.window),
            ).fetchall()
        total = len(rows)
        acc: dict[str, dict[str, Any]] = {}
        for idx, (payload,) in enumerate(rows):
            for item in json.loads(payload):
                key = item["key"]
                entry = acc.setdefault(
                    key,
                    {
                        "key": key,
                        "owner": item["owner"],
                        "zone": item["zone"],
                        "temp": item["temp"],
                        "media": item["media"],
                        "seen": 0,
                        "hashes": set(),
                        "len_sum": 0,
                        "order_sum": 0,
                        "last_seen": idx,
                    },
                )
                entry["seen"] += 1
                entry["hashes"].add(item["hash"])
                entry["len_sum"] += item["len"]
                entry["order_sum"] += item["order"]
        result = {}
        for key, entry in acc.items():
            distinct = len(entry.pop("hashes"))
            seen = entry["seen"]
            entry["distinct"] = distinct
            entry["presence"] = round(seen / total, 4) if total else 0
            entry["avg_len"] = round(entry.pop("len_sum") / seen)
            entry["avg_order"] = entry.pop("order_sum") / seen
            entry["level"] = STATIC if distinct == 1 else OCCASIONAL if distinct <= 5 else DYNAMIC
            result[key] = entry
        return {"requests": total, "segments": result}

    def all_samples(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self.samples)

    def sample(self, sample_id: str) -> dict[str, Any] | None:
        for item in self.all_samples():
            if item.get("id") == sample_id:
                return item
        return None

    def sample_list(self, layout_id: str = "", limit: int = 30) -> list[dict[str, Any]]:
        items = [
            s
            for s in reversed(self.all_samples())
            if not layout_id or s.get("layout_id") == layout_id
        ]
        return [
            {
                "id": s["id"],
                "ts": s["ts"],
                "umo": s["umo"],
                "layout_id": s["layout_id"],
                "title": s.get("title", ""),
                "arranged": s.get("arranged", False),
                "late_changes": sum(1 for c in s.get("changes", []) if c.get("after_arrange")),
            }
            for s in items[:limit]
        ]


# ---------- 警告与推荐（纯计算） ----------


def placement(key: str, owner: str, zone: str, layout: Layout) -> str:
    if pattern_rank(key, owner, layout.system, zone, "system") is not None:
        return "system"
    if pattern_rank(key, owner, layout.tail, zone, "tail") is not None:
        return "tail"
    return zone


def warnings(
    layout: Layout, stats: dict[str, Any], samples: list[dict[str, Any]]
) -> list[dict[str, str]]:
    segs: dict[str, Any] = stats.get("segments", {})
    found: list[dict[str, str]] = []
    if not stats.get("requests"):
        return [{"level": "info", "text": "这个布局还没有统计数据：等它命中几次请求后再看警告。"}]
    sys_entries = []
    for key, entry in segs.items():
        if entry["zone"] == "tools" or entry["media"]:
            continue
        where = placement(key, entry["owner"], entry["zone"], layout)
        if where == "system":
            rank = pattern_rank(key, entry["owner"], layout.system, entry["zone"], "system")
            sys_entries.append((rank if rank is not None else 10**6, entry["avg_order"], entry))
            if entry["level"] == DYNAMIC:
                found.append(
                    {
                        "level": "danger",
                        "text": (
                            f"{key} 每轮都在变，放在 system 会让整个 system "
                            "和全部历史的缓存每轮失效。"
                        ),
                    }
                )
            elif entry["presence"] < 0.95:
                found.append(
                    {
                        "level": "warn",
                        "text": (
                            f"{key} 只在 {entry['presence']:.0%} 的请求里出现，"
                            "它时有时无会让后面的缓存反复失效。"
                        ),
                    }
                )
            if entry["zone"] == "tail" and not entry["temp"]:
                found.append(
                    {
                        "level": "danger",
                        "text": f"{key} 会写入历史，不能移进 system（C1），运行时会退回 tail。",
                    }
                )
        elif (
            where == "tail"
            and entry["temp"]
            and entry["level"] == STATIC
            and entry["presence"] >= 0.95
        ):
            found.append(
                {
                    "level": "tip",
                    "text": (
                        f"{key} 内容固定、每轮都有，却留在 tail 每轮重新计费，"
                        f"可以移到 system 末尾（约 {entry['avg_len']} 字）。"
                    ),
                }
            )
    sys_entries.sort(key=lambda item: (item[0], item[1]))
    stable_after = False
    for _, _, entry in reversed(sys_entries):
        if entry["level"] == STATIC and entry["presence"] >= 0.95:
            stable_after = True
        elif stable_after and entry["level"] != DYNAMIC:
            found.append(
                {
                    "level": "warn",
                    "text": (
                        f"system 里 {entry['key']} 会变化，却排在静态片段前面；"
                        "它一变，后面的静态片段也跟着失效。"
                    ),
                }
            )
    late = {c["owner"] for s in samples for c in s.get("changes", []) if c.get("after_arrange")}
    if late:
        found.append(
            {
                "level": "danger",
                "text": f"{'、'.join(sorted(late))} 在重排之后又修改了请求，这些改动没有参与排序。",
            }
        )
    used = set(layout.system) | set(layout.tail)
    missing = [e for e in used if not e.startswith("@") and not e.endswith("/*") and e not in segs]
    if missing:
        found.append(
            {
                "level": "info",
                "text": (
                    f"布局里有 {len(missing)} 个片段在最近 {stats['requests']} 次请求里"
                    f"都没出现：{'、'.join(missing[:5])}。"
                ),
            }
        )
    unplaced = [
        k
        for k, e in segs.items()
        if e["zone"] != "tools"
        and not e["media"]
        and pattern_rank(k, e["owner"], layout.system, e["zone"], "system") is None
        and pattern_rank(k, e["owner"], layout.tail, e["zone"], "tail") is None
    ]
    if unplaced:
        found.append(
            {
                "level": "info",
                "text": f"有 {len(unplaced)} 个片段还没排，会留在原区的“未排片段”位置。",
            }
        )
    return found


def recommend(layout: Layout, stats: dict[str, Any]) -> Layout:
    """静态且常驻的在前、变化或时有时无的在后；静态 temp 移进 system，动态 system 移到 tail。"""

    segs = [
        e for e in stats.get("segments", {}).values() if e["zone"] != "tools" and not e["media"]
    ]

    def steady(entry: dict[str, Any]) -> bool:
        return entry["level"] == STATIC and entry["presence"] >= 0.95

    system, tail = [], []
    for entry in segs:
        if entry["zone"] == "system":
            keep = entry["level"] != DYNAMIC and entry["presence"] >= 0.95
            (system if keep else tail).append(entry)
        elif entry["temp"] and steady(entry):
            system.append(entry)
        else:
            tail.append(entry)
    rank = {STATIC: 0, OCCASIONAL: 1, DYNAMIC: 2}

    def sort_key(entry: dict[str, Any]) -> tuple:
        return (
            0 if steady(entry) else 1,
            rank[entry["level"]],
            -entry["presence"],
            entry["avg_order"],
        )

    # 会写入历史的 tail 片段保持原相对位置，排在最前，避免改变历史格式。
    saved = sorted(
        (e for e in tail if e["zone"] == "tail" and not e["temp"]), key=lambda e: e["avg_order"]
    )
    rest = sorted((e for e in tail if e not in saved), key=sort_key)
    data = layout.to_dict()
    data["system"] = [e["key"] for e in sorted(system, key=sort_key)] + [UNPLACED]
    data["tail"] = [e["key"] for e in saved + rest] + [UNPLACED, MEDIA]
    return Layout.from_dict(data)
