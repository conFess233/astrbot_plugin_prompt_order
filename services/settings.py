"""读取并校验宿主配置，返回只读快照；页面修改检查器开关时也经由这里保存。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

DEPTHS = ("call", "wire")


@dataclass(frozen=True, slots=True)
class InspectorSettings:
    enabled: bool = True
    depth: str = "wire"
    capture_side: bool = False
    umo_include: tuple[str, ...] = ()
    umo_exclude: tuple[str, ...] = ()
    save_response: bool = True
    fold_media: bool = True
    body_limit_kb: int = 2048
    persist: bool = True
    retention_days: int = 7
    retention_mb: int = 500
    memory_traces: int = 200
    prices: tuple[tuple[str, float, float, float], ...] = ()

    def price_of(self, model: str) -> tuple[float, float, float] | None:
        for name, miss, hit, out in self.prices:
            if name == model:
                return miss, hit, out
        return None


@dataclass(frozen=True, slots=True)
class Settings:
    enabled: bool = True
    dry_run: bool = True
    stats_window: int = 200
    sample_limit: int = 30
    diff_char_limit: int = 50000
    strip_reasoning: bool = False
    inspector: InspectorSettings = InspectorSettings()


# 页面可修改的检查器字段及其校验。
EDITABLE_INSPECTOR = {
    "enabled": bool,
    "depth": str,
    "capture_side": bool,
    "umo_include": str,
    "umo_exclude": str,
    "save_response": bool,
    "fold_media": bool,
    "body_limit_kb": int,
    "persist": bool,
    "retention_days": int,
    "retention_mb": int,
}
RANGES = {
    "body_limit_kb": (64, 16384),
    "retention_days": (1, 90),
    "retention_mb": (50, 10240),
    "memory_traces": (20, 2000),
    "stats_window": (20, 2000),
    "sample_limit": (5, 200),
    "diff_char_limit": (5000, 500000),
}


def _int(value: Any, key: str, default: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    low, high = RANGES[key]
    return max(low, min(high, number))


def _patterns(value: Any) -> tuple[str, ...]:
    text = str(value or "")
    return tuple(p.strip() for p in text.replace(",", "\n").splitlines() if p.strip())


def _prices(value: Any) -> tuple[tuple[str, float, float, float], ...]:
    """解析“模型=未命中,命中,输出”（美元 / 百万 tokens），格式不对的行跳过。"""

    result = []
    for line in str(value or "").splitlines():
        if "=" not in line:
            continue
        name, _, nums = line.partition("=")
        try:
            miss, hit, out = (float(x) for x in nums.split(","))
        except ValueError:
            continue
        if name.strip():
            result.append((name.strip(), miss, hit, out))
    return tuple(result)


def load(config: Any) -> Settings:
    cfg = config or {}
    raw = cfg.get("inspector") or {}
    if not isinstance(raw, dict):
        raw = {}
    depth = str(raw.get("depth", "wire"))
    inspector = InspectorSettings(
        enabled=bool(raw.get("enabled", True)),
        depth=depth if depth in DEPTHS else "wire",
        capture_side=bool(raw.get("capture_side", False)),
        umo_include=_patterns(raw.get("umo_include")),
        umo_exclude=_patterns(raw.get("umo_exclude")),
        save_response=bool(raw.get("save_response", True)),
        fold_media=bool(raw.get("fold_media", True)),
        body_limit_kb=_int(raw.get("body_limit_kb", 2048), "body_limit_kb", 2048),
        persist=bool(raw.get("persist", True)),
        retention_days=_int(raw.get("retention_days", 7), "retention_days", 7),
        retention_mb=_int(raw.get("retention_mb", 500), "retention_mb", 500),
        memory_traces=_int(raw.get("memory_traces", 200), "memory_traces", 200),
        prices=_prices(raw.get("prices")),
    )
    return Settings(
        enabled=bool(cfg.get("enabled", True)),
        dry_run=bool(cfg.get("dry_run", True)),
        stats_window=_int(cfg.get("stats_window", 200), "stats_window", 200),
        sample_limit=_int(cfg.get("sample_limit", 30), "sample_limit", 30),
        diff_char_limit=_int(cfg.get("diff_char_limit", 50000), "diff_char_limit", 50000),
        strip_reasoning=bool(cfg.get("strip_reasoning", False)),
        inspector=inspector,
    )


def apply_inspector_patch(config: Any, patch: Any) -> list[str]:
    """校验页面提交的检查器开关并写回宿主配置，返回改动的字段名。"""

    if not isinstance(patch, dict):
        raise ValueError("提交内容必须是对象")
    current = dict(config.get("inspector") or {})
    changed = []
    for key, value in patch.items():
        kind = EDITABLE_INSPECTOR.get(key)
        if kind is None:
            raise ValueError(f"不支持修改的字段：{key}")
        if kind is bool and not isinstance(value, bool):
            raise ValueError(f"{key} 必须是布尔值")
        if kind is int:
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(f"{key} 必须是整数")
            low, high = RANGES[key]
            if not low <= value <= high:
                raise ValueError(f"{key} 超出范围 {low}～{high}")
        if kind is str:
            value = str(value)
            if key == "depth" and value not in DEPTHS:
                raise ValueError("捕获深度只能是 call 或 wire")
            if len(value) > 2000:
                raise ValueError(f"{key} 不超过 2000 字")
        if current.get(key) != value:
            current[key] = value
            changed.append(key)
    if changed:
        config["inspector"] = current
        config.save_config()
    return changed


def apply_top_patch(config: Any, patch: Any) -> list[str]:
    """页面上的“启用重排 / 试运行 / 清除历史思考”三个总开关。"""

    if not isinstance(patch, dict):
        raise ValueError("提交内容必须是对象")
    changed = []
    for key in ("enabled", "dry_run", "strip_reasoning"):
        if key in patch:
            if not isinstance(patch[key], bool):
                raise ValueError(f"{key} 必须是布尔值")
            if config.get(key) != patch[key]:
                config[key] = patch[key]
                changed.append(key)
    if changed:
        config.save_config()
    return changed
