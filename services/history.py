"""历史压缩：请求前压缩历史里的提醒块与工具结果，回合结束后用小模型异步提炼工具结果。

改写的是 req.contexts，宿主会把它连同本轮新消息一起写回对话历史，所以每段内容只压缩一次，
之后每次请求都发同样的文本，前缀缓存不受影响。压缩发生在上一轮用户消息之后（那里本来就因
临时片段被剥离而重算），不额外打破缓存。
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

from astrbot.api import logger

from .history_compact import (
    Plan,
    ToolItem,
    ToolRules,
    as_dict,
    block_key,
    pending_keys,
    plan_compaction,
    split_blocks,
    targets_of,
)
from .history_store import MEMORY_DIGESTS, DigestStore
from .settings import CompactSettings

ORIGINAL_LIMIT = 60000
PER_TOOL_INPUT = 12000
DISTILL_TIMEOUT = 90.0

SYSTEM_PROMPT = (
    "你负责把聊天机器人调用工具（联网搜索、知识库检索、网页抓取等）拿到的原始结果提炼成简短笔记。"
    "这份笔记会替换原始结果留在对话历史里，供机器人之后回顾。要求：\n"
    "1. 只保留与用户问题和机器人最终回答相关的事实：名称、数字、版本号、日期、结论、关键限定条件；"
    "回答里引用过的信息必须保留。\n"
    "2. 删去广告、导航、重复内容、与问题无关的段落和客套话。\n"
    "3. 不同工具结果互相矛盾时，两种说法都保留并注明来源。\n"
    "4. 末尾列出最多 3 个最有用的来源，格式“来源：标题 链接”；知识库结果写文档名。\n"
    "5. 用简体中文，条目式，不加标题、不加评论、不要写“以下是摘要”之类的开场白。\n"
    "6. 总长度不超过 {limit} 字。"
)


def _combined_original(targets: list[ToolItem]) -> str:
    parts = []
    for i, t in enumerate(targets, 1):
        parts.append(f"### 结果 {i}：{t.name} {t.args}\n{t.text}")
    return "\n\n".join(parts)


def build_prompt(question: str, answer: str, targets: list[ToolItem]) -> str:
    lines = [
        f"用户的问题：{question[:600] or '（未知）'}",
        f"机器人的最终回答：{answer[:1000] or '（无）'}",
        "",
        "工具原始结果：",
    ]
    for i, t in enumerate(targets, 1):
        body = t.text if len(t.text) <= PER_TOOL_INPUT else t.text[:PER_TOOL_INPUT] + "…（后略）"
        lines.append(f"### 结果 {i}：{t.name}({t.args[:200]})\n{body}")
    lines.append("")
    lines.append("请输出提炼后的笔记。")
    return "\n".join(lines)


class HistoryService:
    def __init__(self, data_dir: Path, settings: CompactSettings, context: Any) -> None:
        self.settings = settings
        self.context = context
        self.store = DigestStore(data_dir / "compact.sqlite")
        self._inflight: dict[str, asyncio.Task] = {}
        self._tasks: set[asyncio.Task] = set()
        self._last_prune = 0.0

    def start(self) -> None:
        self.store.open()

    async def stop(self) -> None:
        """停用时取消进行中的提炼，等后台写入结束后关闭数据库。"""

        pending = list(self._inflight.values())
        for task in pending:
            task.cancel()
        for result in await asyncio.gather(*pending, *self._tasks, return_exceptions=True):
            if isinstance(result, Exception):
                logger.warning(f"提示词编排：停用时后台任务出错，原因={result!r}")
        self.store.close()

    def rules(self) -> ToolRules:
        s = self.settings
        return ToolRules(
            min_chars=s.min_chars,
            block_min_chars=s.block_min_chars,
            exclude=s.exclude_tools,
            snippet_chars=s.snippet_chars,
        )

    # ---------- 请求前 ----------

    async def before_request(self, umo: str, req: Any) -> dict[str, Any] | None:
        s = self.settings
        if not s.enabled or not (s.reminders or s.tool_results):
            return None
        contexts = getattr(req, "contexts", None)
        if not isinstance(contexts, list) or not contexts:
            return None
        rules = self.rules()
        waited = 0.0
        if s.tool_results and s.wait_seconds > 0 and self._inflight:
            # 上一轮的提炼还没回来时稍等，避免刚好错过而退回规则截取。
            known = set(self.store.cache)
            keys = [k for k in pending_keys(contexts, rules, known) if k in self._inflight]
            if keys:
                started = time.monotonic()
                # shield：等待超时不取消提炼本身；提炼内部已处理自己的异常。
                tasks = [asyncio.shield(self._inflight[k]) for k in keys]
                await asyncio.wait(tasks, timeout=float(s.wait_seconds))
                waited = time.monotonic() - started
        plan = plan_compaction(
            contexts,
            reminders=s.reminders,
            tools=s.tool_results,
            rules=rules,
            digests=self.store.cache,
        )
        dry = s.dry_run
        if not dry:
            plan.apply()
        record = plan.to_dict()
        record["dry_run"] = dry
        record["waited_ms"] = round(waited * 1000, 1)
        self._background(self._record, umo, dry, plan)
        return record

    def _record(self, umo: str, dry: bool, plan: Plan) -> None:
        try:
            if plan.total_saved or plan.edits:
                self.store.add_event(umo, dry, plan)
            if not dry:
                self.store.mark_applied(plan.keys_used, "digest")
                self.store.mark_applied(plan.keys_trimmed, "trim")
            now = time.time()
            if now - self._last_prune > 3600:
                self._last_prune = now
                self.store.prune()
        except Exception:
            logger.warning("提示词编排：历史压缩记录写入失败", exc_info=True)

    # ---------- 回合结束后 ----------

    def after_turn(self, umo: str, messages: list[Any]) -> None:
        s = self.settings
        if not s.enabled or not s.tool_results or not s.distill_provider_id:
            return
        dicts = [d for d in (as_dict(m) for m in messages or []) if d is not None]
        blocks = split_blocks(dicts)
        if not blocks:
            return
        block = blocks[-1]
        targets = targets_of(block, self.rules())
        if not targets:
            return
        key = block_key(targets)
        if key in self.store.cache or key in self._inflight:
            return
        task = asyncio.create_task(self._distill(key, umo, block.question, block.answer, targets))
        self._inflight[key] = task
        task.add_done_callback(lambda _t, k=key: self._inflight.pop(k, None))

    async def _distill(
        self, key: str, umo: str, question: str, answer: str, targets: list[ToolItem]
    ) -> None:
        s = self.settings
        original = _combined_original(targets)
        row = {
            "key": key,
            "umo": umo,
            "created_at": time.time(),
            "status": "error",
            "model": s.distill_provider_id,
            "tools": [t.name for t in targets],
            "question": question[:600],
            "answer": answer[:2000],
            "original": original[:ORIGINAL_LIMIT],
            "original_chars": sum(len(t.text) for t in targets),
            "digest": "",
            "digest_chars": 0,
            "duration_ms": None,
            "error": "",
        }
        started = time.monotonic()
        try:
            resp = await asyncio.wait_for(
                self.context.llm_generate(
                    chat_provider_id=s.distill_provider_id,
                    prompt=build_prompt(question, answer, targets),
                    system_prompt=SYSTEM_PROMPT.format(limit=s.digest_max_chars),
                ),
                timeout=DISTILL_TIMEOUT,
            )
            text = str(getattr(resp, "completion_text", "") or "").strip()
            hard = int(s.digest_max_chars * 1.5)
            if len(text) > hard:
                text = text[:hard].rstrip() + "…"
            if not text:
                row["error"] = "模型返回为空"
            elif len(text) >= row["original_chars"]:
                row["error"] = "摘要不比原文短，已放弃"
            else:
                row["status"] = "ok"
                row["digest"] = text
                row["digest_chars"] = len(text)
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError as exc:
            # 3.11 起 asyncio.TimeoutError 就是 TimeoutError，模型自己抛的超时也会进这里。
            if time.monotonic() - started >= DISTILL_TIMEOUT - 1:
                row["error"] = f"超时（{int(DISTILL_TIMEOUT)} 秒）"
            else:
                row["error"] = f"TimeoutError: {exc}"[:500]
        except Exception as exc:
            row["error"] = f"{type(exc).__name__}: {exc}"[:500]
        row["duration_ms"] = round((time.monotonic() - started) * 1000, 1)
        if row["status"] == "ok":
            self.store.cache[key] = row["digest"]
            if len(self.store.cache) > MEMORY_DIGESTS:
                self.store.cache.pop(next(iter(self.store.cache)))
        else:
            logger.warning(f"提示词编排：工具结果提炼失败，会话={umo}，原因={row['error']}")
        try:
            await asyncio.to_thread(self.store.save_digest, row)
        except Exception:
            logger.warning("提示词编排：摘要写入失败", exc_info=True)

    def _background(self, fn, *args) -> None:
        task = asyncio.create_task(asyncio.to_thread(fn, *args))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def status(self) -> dict[str, Any]:
        return {"inflight": len(self._inflight), "cached": len(self.store.cache)}
