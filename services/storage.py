"""布局配置的持久化：当前版本、历史版本、原子写入与并发保护。"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any

from astrbot.api import logger

from ..models.layout import Layout, LayoutError, default_layout

KEEP_VERSIONS = 50


class StaleVersionError(RuntimeError):
    """保存时基于的版本已不是最新。"""


def atomic_write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


class LayoutStore:
    """读写 config.json 与 versions/；所有方法都是同步的，调用方放到线程里执行。"""

    def __init__(self, data_dir: Path) -> None:
        self.path = data_dir / "config.json"
        self.versions_dir = data_dir / "versions"
        self._lock = threading.Lock()
        self.version = 0
        self.updated_at = 0.0
        self.layouts: list[Layout] = [default_layout()]

    def load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            layouts = [Layout.from_dict(item) for item in data.get("layouts", [])]
        except (OSError, ValueError, LayoutError):
            # 不覆盖损坏的文件，保留给用户手工恢复。
            logger.warning("提示词编排：布局配置读取失败，暂用默认布局", exc_info=True)
            return
        self.layouts = layouts or [default_layout()]
        self.version = int(data.get("version", 0))
        self.updated_at = float(data.get("updated_at", 0))

    def snapshot(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "updated_at": self.updated_at,
            "layouts": [layout.to_dict() for layout in self.layouts],
        }

    def save(self, raw_layouts: Any, base_version: int, label: str = "") -> int:
        """校验并保存为新版本；基于的版本过期时抛出 StaleVersionError。"""

        if not isinstance(raw_layouts, list) or not raw_layouts or len(raw_layouts) > 50:
            raise LayoutError("布局列表必须包含 1～50 个布局")
        layouts = [Layout.from_dict(item) for item in raw_layouts]
        ids = [layout.id for layout in layouts]
        if len(set(ids)) != len(ids):
            raise LayoutError("布局 ID 不能重复")
        with self._lock:
            if base_version != self.version:
                raise StaleVersionError(f"配置已被其他页面更新到 v{self.version}，请刷新后再保存")
            version = self.version + 1
            now = time.time()
            data = {
                "version": version,
                "updated_at": now,
                "label": label[:60],
                "layouts": [layout.to_dict() for layout in layouts],
            }
            atomic_write_json(self.versions_dir / f"v{version:05d}.json", data)
            atomic_write_json(self.path, data)
            self.layouts, self.version, self.updated_at = layouts, version, now
            self._prune()
            return version

    def versions(self) -> list[dict[str, Any]]:
        items = []
        for path in sorted(self.versions_dir.glob("v*.json"), reverse=True):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            items.append(
                {
                    "version": data.get("version"),
                    "updated_at": data.get("updated_at"),
                    "label": data.get("label", ""),
                    "layouts": len(data.get("layouts", [])),
                }
            )
        return items

    def rollback(self, version: int, base_version: int) -> int:
        path = self.versions_dir / f"v{int(version):05d}.json"
        if not path.exists():
            raise LayoutError(f"版本 v{version} 不存在")
        data = json.loads(path.read_text(encoding="utf-8"))
        return self.save(data.get("layouts", []), base_version, f"回滚到 v{version}")

    def _prune(self) -> None:
        files = sorted(self.versions_dir.glob("v*.json"))
        for path in files[:-KEEP_VERSIONS]:
            try:
                path.unlink()
            except OSError:
                logger.warning(f"提示词编排：清理旧版本失败，文件={path.name}")
