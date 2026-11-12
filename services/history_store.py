"""历史压缩的存储：摘要原文与提炼结果、每次压缩的节省量（SQLite），以及页面查询。"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from astrbot.api import logger

from .history_compact import Plan

KEEP_DAYS = 30
MEMORY_DIGESTS = 2000


class DigestStore:
    """摘要与压缩事件的 SQLite 存储；内存里缓存最近的摘要供请求钩子同步查询。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._conn: sqlite3.Connection | None = None
        self._lock = threading.Lock()
        self.cache: dict[str, str] = {}

    def open(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS digests ("
            "key TEXT PRIMARY KEY, umo TEXT, created_at REAL, status TEXT, model TEXT, "
            "tools TEXT, question TEXT, answer TEXT, original TEXT, original_chars INTEGER, "
            "digest TEXT, digest_chars INTEGER, duration_ms REAL, error TEXT, "
            "applied TEXT DEFAULT '', applied_at REAL)"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS events ("
            "ts REAL, umo TEXT, dry INTEGER, saved INTEGER, new_saved INTEGER, payload TEXT)"
        )
        conn.execute("CREATE INDEX IF NOT EXISTS events_ts ON events(ts)")
        conn.execute("CREATE INDEX IF NOT EXISTS digests_ts ON digests(created_at)")
        conn.commit()
        self._conn = conn
        rows = conn.execute(
            "SELECT key, digest FROM digests WHERE status='ok' ORDER BY created_at DESC LIMIT ?",
            (MEMORY_DIGESTS,),
        ).fetchall()
        # 旧的在前，超出上限时先淘汰旧的。
        self.cache = {k: d for k, d in reversed(rows) if d}

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    def _exec(self, sql: str, args: tuple = ()) -> list[tuple]:
        with self._lock:
            if self._conn is None:
                return []
            rows = self._conn.execute(sql, args).fetchall()
            self._conn.commit()
            return rows

    def save_digest(self, row: dict[str, Any]) -> None:
        self._exec(
            "INSERT OR REPLACE INTO digests (key, umo, created_at, status, model, tools, question, "
            "answer, original, original_chars, digest, digest_chars, duration_ms, error) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                row["key"],
                row["umo"],
                row["created_at"],
                row["status"],
                row["model"],
                json.dumps(row["tools"], ensure_ascii=False),
                row["question"],
                row["answer"],
                row["original"],
                row["original_chars"],
                row["digest"],
                row["digest_chars"],
                row["duration_ms"],
                row["error"],
            ),
        )

    def mark_applied(self, keys: list[str], how: str) -> None:
        now = time.time()
        for key in keys:
            self._exec(
                "UPDATE digests SET applied=?, applied_at=? WHERE key=? AND applied=''",
                (how, now, key),
            )

    def add_event(self, umo: str, dry: bool, plan: Plan) -> None:
        self._exec(
            "INSERT INTO events (ts, umo, dry, saved, new_saved, payload) VALUES (?,?,?,?,?,?)",
            (
                time.time(),
                umo,
                int(dry),
                plan.total_saved,
                plan.new_saved,
                json.dumps(plan.to_dict(), ensure_ascii=False),
            ),
        )

    def prune(self) -> None:
        cutoff = time.time() - KEEP_DAYS * 86400
        self._exec("DELETE FROM events WHERE ts < ?", (cutoff,))
        self._exec("DELETE FROM digests WHERE created_at < ?", (cutoff,))

    # ---------- 页面查询 ----------

    def overview(self, since: float) -> dict[str, Any]:
        ev = self._exec(
            "SELECT dry, COUNT(*), COALESCE(SUM(saved),0), COALESCE(SUM(new_saved),0) "
            "FROM events WHERE ts >= ? GROUP BY dry",
            (since,),
        )
        events = {
            "live" if not dry else "dry": {"requests": n, "saved": s, "new_saved": ns}
            for dry, n, s, ns in ev
        }
        kinds: dict[str, int] = {}
        for (payload,) in self._exec("SELECT payload FROM events WHERE ts >= ?", (since,)):
            try:
                data = json.loads(payload)
            except ValueError:
                logger.warning("提示词编排：历史压缩统计中有一条记录无法解析，已跳过")
                continue
            for key in ("reminders", "digest_blocks", "trim_items"):
                kinds[key] = kinds.get(key, 0) + int(data.get(key, 0) or 0)
        dg = self._exec(
            "SELECT status, COUNT(*), COALESCE(SUM(original_chars),0), "
            "COALESCE(SUM(digest_chars),0), COALESCE(AVG(duration_ms),0) "
            "FROM digests WHERE created_at >= ? GROUP BY status",
            (since,),
        )
        digests = {
            status: {"count": n, "original": o, "digest": d, "avg_ms": round(ms, 1)}
            for status, n, o, d, ms in dg
        }
        return {"events": events, "kinds": kinds, "digests": digests}

    def recent(self, limit: int) -> list[dict[str, Any]]:
        rows = self._exec(
            "SELECT key, umo, created_at, status, model, tools, question, original_chars, "
            "digest_chars, duration_ms, error, applied FROM digests "
            "ORDER BY created_at DESC LIMIT ?",
            (limit,),
        )
        keys = (
            "key",
            "umo",
            "created_at",
            "status",
            "model",
            "tools",
            "question",
            "original_chars",
            "digest_chars",
            "duration_ms",
            "error",
            "applied",
        )
        return [_api_item(dict(zip(keys, row, strict=True))) for row in rows]

    def detail(self, key: str) -> dict[str, Any] | None:
        rows = self._exec(
            "SELECT key, umo, created_at, status, model, tools, question, answer, original, "
            "original_chars, digest, digest_chars, duration_ms, error, applied, applied_at "
            "FROM digests WHERE key=?",
            (key,),
        )
        if not rows:
            return None
        keys = (
            "key",
            "umo",
            "created_at",
            "status",
            "model",
            "tools",
            "question",
            "answer",
            "original",
            "original_chars",
            "digest",
            "digest_chars",
            "duration_ms",
            "error",
            "applied",
            "applied_at",
        )
        return _api_item(dict(zip(keys, rows[0], strict=True)))


def _api_item(item: dict[str, Any]) -> dict[str, Any]:
    """接口输出：顶层不能有 status 字段，宿主页面会把 status=error 当成请求失败。"""

    item["state"] = item.pop("status")
    try:
        item["tools"] = json.loads(item["tools"] or "[]")
    except (ValueError, TypeError):
        logger.warning(f"提示词编排：摘要记录的工具列表无法解析，已按空列表显示，key={item['key']}")
        item["tools"] = []
    return item
