"""应用状态：媒体列表 + 下载任务 + 配置。"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from sniffer import dedupe_key, filename_for, human_size
from proxy import MediaHit

MAX_MEDIA = 200


class AppState:
    def __init__(self, config: dict):
        self.config = config
        self.media: dict[str, dict] = {}
        self.tasks: dict[str, object] = {}
        self.lock = threading.Lock()
        self.proxy = None
        self.events: list[str] = []
        self.started_at = time.time()

    # ---------------- 媒体 ----------------

    def add_media(self, hit: MediaHit) -> str:
        key = dedupe_key(hit.url, hit.kind)
        with self.lock:
            old = self.media.get(key)
            if old:
                if hit.size:
                    old["size"] = hit.size
                if hit.mime:
                    old["mime"] = hit.mime
                old["at"] = hit.at
                old["hits"] = old.get("hits", 1) + 1
            else:
                self.media[key] = {
                    "id": key,
                    "url": hit.url,
                    "kind": hit.kind,
                    "mime": hit.mime,
                    "size": hit.size,
                    "size_text": human_size(hit.size),
                    "referer": hit.referer,
                    "origin": hit.origin,
                    "filename": hit.filename or filename_for(hit.url, hit.kind),
                    "at": hit.at,
                    "hits": 1,
                }
                if len(self.media) > MAX_MEDIA:
                    oldest = min(self.media.values(), key=lambda x: x["at"])
                    self.media.pop(oldest["id"], None)
        return key

    def list_media(self) -> list[dict]:
        with self.lock:
            items = list(self.media.values())
        items.sort(key=lambda x: x["at"], reverse=True)
        return items

    def clear_media(self) -> None:
        with self.lock:
            self.media.clear()

    # ---------------- 任务 ----------------

    def add_task(self, task) -> None:
        with self.lock:
            self.tasks[task.id] = task

    def get_task(self, task_id: str):
        with self.lock:
            return self.tasks.get(task_id)

    def list_tasks(self) -> list[dict]:
        with self.lock:
            tasks = list(self.tasks.values())
        out = [t.snapshot() for t in tasks]
        out.sort(key=lambda x: x.get("id", ""), reverse=True)
        return out

    def clear_finished(self) -> None:
        with self.lock:
            for tid, t in list(self.tasks.items()):
                if t.state in ("done", "error"):
                    self.tasks.pop(tid, None)

    # ---------------- 快照 ----------------

    def snapshot(self) -> dict:
        return {
            "media": self.list_media(),
            "tasks": self.list_tasks(),
            "config": {
                "dest_dir": str(self.config.get("dest_dir", "")),
                "threads": self.config.get("threads", 8),
                "port": self.config.get("proxy_port", 8899),
            },
            "stats": dict(getattr(self.proxy, "stats", {})),
            "uptime": int(time.time() - self.started_at),
        }


def load_config(base: Path) -> dict:
    cfg_path = base / "config.json"
    defaults = {
        "dest_dir": str(base / "downloads"),
        "threads": 8,
        "proxy_port": 8899,
        "ui_port": 8900,
        "auto_proxy": False,
        "max_concurrent_tasks": 2,
    }
    if cfg_path.exists():
        try:
            data = json.loads(cfg_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                defaults.update({k: v for k, v in data.items() if k in defaults})
        except Exception:
            pass
    Path(defaults["dest_dir"]).mkdir(parents=True, exist_ok=True)
    return defaults


def save_config(base: Path, config: dict) -> None:
    try:
        (base / "config.json").write_text(
            json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception:
        pass
