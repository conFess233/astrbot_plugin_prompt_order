"""检查器记录的内存缓存与落盘：按天 JSONL + SQLite 索引 + 保留策略 + 进行中记录恢复。"""

from __future__ import annotations

import asyncio
import contextlib
import json
import sqlite3
import threading
import time
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any

from astrbot.api import logger

from ..models.trace import Trace
from .storage import atomic_write_json

FLUSH_SECONDS = 1.0
INFLIGHT_SECONDS = 5.0
RETENTION_SECONDS = 3600.0
INDEX_COLUMNS = (
    "id, ts, ended, umo, kind, title, models, rounds, input, cached, output, "
    "error, status, file, offset, length"
)


class TraceStore:
    """内存里保留最近的记录；落盘开启时，结束的记录批量写入当天文件并建立索引。"""

    def __init__(self, root: Path, memory_limit: int) -> None:
        self.root = root
        self.traces_dir = root / "traces"
        self.index_path = root / "index.sqlite"
        self.inflight_path = root / "inflight.json"
        self.memory_limit = memory_limit
        self.persist = True
        self.retention_days = 7
        self.retention_mb = 500
        self.recent: OrderedDict[str, Trace] = OrderedDict()
        self._pending: list[Trace] = []
        # 已写进 jsonl、但索引还没写成功的行；下次写盘先补索引，不重复写文件。
        self._unindexed: list[tuple] = []
        self._lock = threading.Lock()
        # 写盘与清空互斥，避免清空后又补进指向已删文件的索引。
        self._flush_lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None
        self._task: asyncio.Task | None = None
        self._last_retention = 0.0
        self._last_inflight = 0.0
        self._oversize_warned = ""

    # ---------- 生命周期 ----------

    def open(self) -> None:
        self.traces_dir.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.index_path, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS traces (id TEXT PRIMARY KEY, ts REAL, ended REAL, "
            "umo TEXT, kind TEXT, title TEXT, models TEXT, rounds INTEGER, input INTEGER, "
            "cached INTEGER, output INTEGER, error INTEGER, status TEXT, file TEXT, "
            "offset INTEGER, length INTEGER)"
        )
        conn.execute("CREATE INDEX IF NOT EXISTS traces_ts ON traces(ts)")
        conn.commit()
        self._conn = conn
        self._recover_inflight()

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        # 停用时把进行中的记录也写进 inflight，下次启动补写为“中断”。
        await asyncio.to_thread(self._write_inflight)
        await asyncio.to_thread(self._flush)
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    # ---------- 写入 ----------

    def add(self, trace: Trace) -> None:
        self.recent[trace.id] = trace
        self.recent.move_to_end(trace.id)
        while len(self.recent) > self.memory_limit:
            oldest_id, oldest = next(iter(self.recent.items()))
            if oldest.status == "open":
                # 进行中的记录不能挤掉；挪到末尾再看下一条。
                self.recent.move_to_end(oldest_id)
                if all(t.status == "open" for t in self.recent.values()):
                    break
                continue
            self.recent.pop(oldest_id)

    def finish(self, trace: Trace) -> None:
        if trace.status == "open":
            trace.status = "done"
        trace.ended_at = trace.ended_at or time.time()
        self.add(trace)
        if self.persist:
            with self._lock:
                self._pending.append(trace)

    def open_traces(self) -> list[Trace]:
        return [t for t in self.recent.values() if t.status == "open"]

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(FLUSH_SECONDS)
            try:
                await asyncio.to_thread(self._flush)
                now = time.monotonic()
                if now - self._last_inflight >= INFLIGHT_SECONDS:
                    self._last_inflight = now
                    await asyncio.to_thread(self._write_inflight)
                if now - self._last_retention >= RETENTION_SECONDS:
                    self._last_retention = now
                    await asyncio.to_thread(self.enforce_retention)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("提示词编排：检查器写盘失败，稍后重试", exc_info=True)

    def _flush(self) -> None:
        with self._flush_lock:
            self._flush_locked()

    def _flush_locked(self) -> None:
        if self._conn is None:
            return
        with self._lock:
            batch, self._pending = self._pending, []
        if not batch and not self._unindexed:
            return
        rows: list[tuple] = []
        failed: OSError | None = None
        for done, trace in enumerate(batch):
            try:
                data = trace.to_dict()
                line = (json.dumps(data, ensure_ascii=False, default=str) + "\n").encode("utf-8")
                day = datetime.fromtimestamp(trace.started_at).strftime("%Y-%m-%d")
                path = self.traces_dir / f"{day}.jsonl"
                with path.open("ab") as fh:
                    offset = fh.tell()
                    fh.write(line)
            except OSError as exc:
                # 写失败的记录放回队列，保留在内存里等下次；已写成功的照常建索引。
                with self._lock:
                    self._pending = batch[done:] + self._pending
                failed = exc
                break
            rows.append(_index_row(data, path.name, offset, len(line)))
        self._unindexed.extend(rows)
        if self._unindexed:
            with self._lock:
                try:
                    self._conn.executemany(
                        f"INSERT OR REPLACE INTO traces ({INDEX_COLUMNS}) VALUES "
                        "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        self._unindexed,
                    )
                    self._conn.commit()
                except sqlite3.Error:
                    self._conn.rollback()
                    raise
            self._unindexed = []
        if failed is not None:
            raise failed

    def _write_inflight(self) -> None:
        if not self.persist:
            return
        data = [t.to_dict() for t in self.open_traces()]
        if data:
            atomic_write_json(self.inflight_path, data)
        elif self.inflight_path.exists():
            self.inflight_path.unlink()

    def _recover_inflight(self) -> None:
        if not self.inflight_path.exists():
            return
        try:
            items = json.loads(self.inflight_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            logger.warning("提示词编排：进行中记录文件损坏，已跳过", exc_info=True)
            return
        recovered = 0
        for data in items if isinstance(items, list) else []:
            try:
                trace = _trace_from_dict(data)
            except (TypeError, ValueError, KeyError, AttributeError):
                logger.warning("提示词编排：一条进行中记录格式不对，已跳过", exc_info=True)
                continue
            trace.status = "interrupted"
            trace.ended_at = trace.ended_at or trace.touched_at
            self._pending.append(trace)
            recovered += 1
        self._flush()
        self.inflight_path.unlink(missing_ok=True)
        if recovered:
            logger.info(f"提示词编排：补写了 {recovered} 条重启前未完成的记录")

    # ---------- 读取 ----------

    def memory_summaries(self) -> list[dict[str, Any]]:
        """内存中记录的摘要。必须在事件循环线程调用：recent 只在该线程被修改。"""

        return [trace.summary() for trace in list(self.recent.values())]

    def query(
        self,
        filters: dict[str, Any],
        limit: int,
        before: float | None,
        memory: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        """内存摘要与索引合并后筛选分页；可在线程中调用（memory 由调用方预先取好）。"""

        merged: dict[str, dict[str, Any]] = {}
        for summary in self.memory_summaries() if memory is None else memory:
            merged[summary["id"]] = summary
        if self._conn is not None:
            # 所有筛选条件都下推到 SQL，保证每页取满，不会因事后过滤而提前判断“没有更多”。
            sql, args = _filter_sql(filters, before)
            sql += " ORDER BY ts DESC LIMIT ?"
            args.append(limit)
            with self._lock:
                rows = self._conn.execute(sql, args).fetchall()
            for row in rows:
                summary = _summary_from_row(row)
                merged.setdefault(summary["id"], summary)
        items = [s for s in merged.values() if _match(s, filters, before)]
        items.sort(key=lambda s: s["started_at"], reverse=True)
        return items[:limit]

    def get(self, trace_id: str) -> dict[str, Any] | None:
        """先查内存再查磁盘；只在事件循环线程调用。线程中请用 get_persisted。"""

        trace = self.recent.get(trace_id)
        if trace is not None:
            return trace.to_dict()
        return self.get_persisted(trace_id)

    def get_persisted(self, trace_id: str) -> dict[str, Any] | None:
        if self._conn is None:
            return None
        with self._lock:
            row = self._conn.execute(
                "SELECT file, offset, length FROM traces WHERE id = ?", (trace_id,)
            ).fetchone()
        if row is None:
            return None
        path = self.traces_dir / row[0]
        try:
            with path.open("rb") as fh:
                fh.seek(row[1])
                return json.loads(fh.read(row[2]))
        except (OSError, ValueError):
            logger.warning(f"提示词编排：读取记录失败，id={trace_id}", exc_info=True)
            return None

    def stats(
        self,
        since: float,
        memory: list[dict[str, Any]] | None = None,
        until: float | None = None,
        tz_minutes: int | None = None,
    ) -> dict[str, Any]:
        """按天、按类型、按模型、按会话汇总 [since, until)；进行中的记录不计入。可在线程中调用。

        tz_minutes 是页面所在时区相对 UTC 的分钟数，按天分组用它；不传时用服务器时区。
        """

        end = float("inf") if until is None else until
        tz = None if tz_minutes is None else timezone(timedelta(minutes=tz_minutes))

        rows: list[tuple] = []
        seen: set[str] = set()
        # 关闭落盘后磁盘上的旧记录仍在列表里，汇总同样计入，两边口径一致。
        if self._conn is not None:
            with self._lock:
                rows = self._conn.execute(
                    "SELECT id, ts, umo, kind, models, rounds, input, cached, output, error "
                    "FROM traces WHERE ts >= ? AND ts < ?",
                    (since, end),
                ).fetchall()
            seen = {row[0] for row in rows}
        # 刚结束的记录最多要等一秒才写进索引，内存里有而索引里没有的也要算上。
        for s in self.memory_summaries() if memory is None else memory:
            if since <= s["started_at"] < end and s["status"] != "open" and s["id"] not in seen:
                rows.append(
                    (
                        s["id"],
                        s["started_at"],
                        s["umo"],
                        s["kind"],
                        ",".join(s["models"]),
                        s["rounds"],
                        s["input"],
                        s["input_cached"],
                        s["output"],
                        int(s["error"]),
                    )
                )
        total = _bucket()
        by_kind = {"main": _bucket(), "side": _bucket()}
        # 合计与主模型 / 旁路各一套按天、按模型、按会话的拆分，页面切换范围时直接取用。
        groups = {
            scope: {"by_day": {}, "by_model": {}, "by_umo": {}}
            for scope in ("total", "main", "side")
        }
        for _id, ts, umo, kind, models, rounds, inp, cached, out, err in rows:
            day = datetime.fromtimestamp(ts, tz).strftime("%Y-%m-%d")
            scope = "main" if kind == "main" else "side"
            buckets = [total, by_kind[scope]]
            for name in ("total", scope):
                group = groups[name]
                buckets += [
                    group["by_day"].setdefault(day, _bucket()),
                    group["by_model"].setdefault(models or "-", _bucket()),
                    group["by_umo"].setdefault(umo or "-", _bucket()),
                ]
            for bucket in buckets:
                bucket["count"] += 1
                bucket[scope] += 1
                bucket["rounds"] += rounds or 0
                bucket["input"] += inp or 0
                bucket["cached"] += cached or 0
                bucket["output"] += out or 0
                bucket["errors"] += err or 0
        return {
            "total": total,
            "by_kind": by_kind,
            **groups["total"],
            "by_scope": {"main": groups["main"], "side": groups["side"]},
        }

    def disk_usage(self) -> int:
        total = 0
        for path in self.root.glob("**/*"):
            if path.is_file():
                with contextlib.suppress(OSError):
                    total += path.stat().st_size
        return total

    # ---------- 维护 ----------

    def enforce_retention(self) -> int:
        files = sorted(self.traces_dir.glob("*.jsonl"))
        cutoff = datetime.fromtimestamp(time.time() - self.retention_days * 86400).strftime(
            "%Y-%m-%d"
        )
        removed = [p for p in files if p.stem < cutoff]
        keep = [p for p in files if p not in removed]
        limit = self.retention_mb * 1024 * 1024
        sizes = {p: p.stat().st_size for p in keep}
        while keep and sum(sizes[p] for p in keep) > limit and len(keep) > 1:
            removed.append(keep.pop(0))
        if len(keep) == 1 and sizes[keep[0]] > limit and self._oversize_warned != keep[0].stem:
            # 只剩当天的文件时不会删除正在写入的记录，提示一次即可。
            self._oversize_warned = keep[0].stem
            logger.warning(
                f"提示词编排：当天记录已超过总大小上限 {self.retention_mb}MB，"
                "可调大上限、关闭旁路调用记录或开启图片折叠"
            )
        for path in removed:
            try:
                path.unlink()
            except OSError:
                logger.warning(f"提示词编排：删除过期记录文件失败，文件={path.name}")
                continue
            if self._conn is not None:
                with self._lock:
                    self._conn.execute("DELETE FROM traces WHERE file = ?", (path.name,))
                    self._conn.commit()
        if removed:
            logger.info(f"提示词编排：按保留策略删除了 {len(removed)} 个记录文件")
        return len(removed)

    def clear_memory(self) -> int:
        """清掉内存中已结束的记录；只在事件循环线程调用。"""

        count = sum(1 for t in self.recent.values() if t.status != "open")
        self.recent = OrderedDict((k, t) for k, t in self.recent.items() if t.status == "open")
        return count

    def clear_disk(self) -> int:
        count = 0
        with self._flush_lock:
            with self._lock:
                self._pending.clear()
                self._unindexed.clear()
                if self._conn is not None:
                    count = self._conn.execute("SELECT COUNT(*) FROM traces").fetchone()[0]
                    self._conn.execute("DELETE FROM traces")
                    self._conn.commit()
            for path in self.traces_dir.glob("*.jsonl"):
                try:
                    path.unlink()
                except OSError:
                    logger.warning(f"提示词编排：清空时删除文件失败，文件={path.name}")
        return count


def _bucket() -> dict[str, int]:
    return {
        "count": 0,
        "main": 0,
        "side": 0,
        "rounds": 0,
        "input": 0,
        "cached": 0,
        "output": 0,
        "errors": 0,
    }


def _index_row(data: dict[str, Any], file: str, offset: int, length: int) -> tuple:
    return (
        data["id"],
        data["started_at"],
        data.get("ended_at"),
        data.get("umo", ""),
        data["kind"],
        data.get("title", ""),
        ",".join(data.get("models", [])),
        data.get("rounds", 0),
        data.get("input", 0),
        data.get("input_cached", 0),
        data.get("output", 0),
        int(bool(data.get("error"))),
        data.get("status", "done"),
        file,
        offset,
        length,
    )


def _filter_sql(filters: dict[str, Any], before: float | None) -> tuple[str, list[Any]]:
    sql = f"SELECT {INDEX_COLUMNS} FROM traces WHERE 1=1"
    args: list[Any] = []
    if before is not None:
        sql += " AND ts < ?"
        args.append(before)
    if filters.get("since"):
        sql += " AND ts >= ?"
        args.append(float(filters["since"]))
    if filters.get("until"):
        sql += " AND ts < ?"
        args.append(float(filters["until"]))
    if filters.get("kind"):
        sql += " AND kind = ?"
        args.append(filters["kind"])
    if filters.get("errors"):
        sql += " AND error = 1"
    if filters.get("umo"):
        # GLOB 与 fnmatchcase 一样区分大小写，支持 * ? []
        sql += " AND umo GLOB ?"
        args.append(filters["umo"])
    if filters.get("model"):
        sql += " AND instr(',' || models || ',', ',' || ? || ',') > 0"
        args.append(filters["model"])
    if filters.get("min_tokens"):
        sql += " AND input >= ?"
        args.append(int(filters["min_tokens"]))
    if filters.get("low_hit"):
        sql += " AND input > 0 AND cached * 1.0 / input < 0.5"
    if filters.get("q"):
        sql += " AND (instr(lower(title), lower(?)) > 0 OR instr(lower(umo), lower(?)) > 0)"
        args += [filters["q"], filters["q"]]
    return sql, args


def _summary_from_row(row: tuple) -> dict[str, Any]:
    (tid, ts, ended, umo, kind, title, models, rounds, inp, cached, out, err, status, *_rest) = row
    return {
        "id": tid,
        "kind": kind,
        "umo": umo,
        "title": title,
        "sender": "",
        "layout_id": "",
        "started_at": ts,
        "ended_at": ended,
        "status": status,
        "rounds": rounds,
        "models": [m for m in (models or "").split(",") if m],
        "input": inp,
        "input_cached": cached,
        "output": out,
        "hit_rate": round(cached / inp, 4) if inp else None,
        "duration_ms": round(((ended or ts) - ts) * 1000, 1),
        "error": bool(err),
    }


def _match(summary: dict[str, Any], filters: dict[str, Any], before: float | None) -> bool:
    if before is not None and summary["started_at"] >= before:
        return False
    if filters.get("since") and summary["started_at"] < float(filters["since"]):
        return False
    if filters.get("until") and summary["started_at"] >= float(filters["until"]):
        return False
    if filters.get("kind") and summary["kind"] != filters["kind"]:
        return False
    if filters.get("errors") and not summary["error"]:
        return False
    if filters.get("umo") and not fnmatchcase(summary["umo"] or "", filters["umo"]):
        return False
    if filters.get("model") and filters["model"] not in summary["models"]:
        return False
    if filters.get("min_tokens") and (summary["input"] or 0) < int(filters["min_tokens"]):
        return False
    if filters.get("low_hit"):
        rate = summary.get("hit_rate")
        if rate is None or rate >= 0.5:
            return False
    keyword = filters.get("q")
    return not keyword or keyword.lower() in f"{summary['title']} {summary['umo']}".lower()


def _trace_from_dict(data: dict[str, Any]) -> Trace:
    """恢复进行中记录：只保留摘要所需字段和原始轮次数据。"""

    from ..models.trace import Attempt, Round

    trace = Trace(kind=data.get("kind", "main"))
    for key in ("id", "umo", "title", "sender", "layout_id", "started_at", "ended_at", "build"):
        if key in data:
            setattr(trace, key, data[key])
    trace.touched_at = data.get("ended_at") or data.get("started_at") or time.time()
    for item in data.get("round_list", []):
        attempts = [Attempt(**a) for a in item.pop("attempts", [])]
        trace.rounds.append(Round(**item, attempts=attempts))
    return trace
