"""多线程分段下载引擎。

IDM 的核心就是这套东西：把一个文件切成若干段并发拉，每段支持断点续传。
这里用标准库实现，不依赖 requests。

要点：
- 先用 Range: bytes=0-0 探测是否支持分段（HEAD 经常被站点拒绝）
- 支持分段就切 N 段并发；不支持就退化成单线程
- 每段的进度写进 .vhmeta，进程重启后能从断点接着下
"""

from __future__ import annotations

import json
import os
import shutil
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable

UA_DEFAULT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
MIN_SEGMENT = 256 * 1024  # 小于 256KB 的段没意义


class DownloadError(RuntimeError):
    pass


def _open(url: str, headers: dict[str, str], timeout: int = 30):
    req = urllib.request.Request(url, headers=headers)
    return urllib.request.urlopen(req, timeout=timeout)


def probe(url: str, referer: str = "", user_agent: str = "") -> dict:
    """探测文件大小、是否支持 Range、最终 URL。"""
    headers = {
        "User-Agent": user_agent or UA_DEFAULT,
        "Accept": "*/*",
        "Accept-Encoding": "identity",
        "Range": "bytes=0-0",
    }
    if referer:
        headers["Referer"] = referer

    try:
        resp = _open(url, headers, timeout=20)
        with resp:
            status = getattr(resp, "status", 200)
            final_url = resp.geturl()
            cr = resp.headers.get("Content-Range", "")
            cl = resp.headers.get("Content-Length", "")
            accept = resp.headers.get("Accept-Ranges", "").lower()
            ctype = resp.headers.get("Content-Type", "")

        total = 0
        if "/" in cr:
            tail = cr.rsplit("/", 1)[-1].strip()
            if tail.isdigit():
                total = int(tail)
        if not total and cl.isdigit() and status != 206:
            total = int(cl)
        if status == 206 and not total and cl.isdigit():
            total = int(cl)

        return {
            "ok": True,
            "status": status,
            "url": final_url,
            "size": total,
            "range": status == 206 or accept == "bytes",
            "mime": ctype.split(";")[0].strip(),
        }
    except urllib.error.HTTPError as e:
        # 有些服务器不支持 Range 会回 416，但文件本身能下
        if e.code == 416:
            return {"ok": True, "status": 416, "url": url, "size": 0, "range": False, "mime": ""}
        return {"ok": False, "error": f"HTTP {e.code}", "url": url}
    except Exception as e:
        return {"ok": False, "error": str(e), "url": url}


class DownloadTask:
    """一个下载任务。线程安全，可从任意线程读状态。"""

    def __init__(
        self,
        url: str,
        dest_dir: str | Path,
        filename: str,
        referer: str = "",
        user_agent: str = "",
        threads: int = 8,
        on_progress: Callable[["DownloadTask"], None] | None = None,
    ):
        self.url = url
        self.dest_dir = Path(dest_dir)
        self.filename = self._unique_name(filename)
        self.referer = referer
        self.user_agent = user_agent or UA_DEFAULT
        self.threads = max(1, min(16, threads))
        self.on_progress = on_progress

        self.id = f"dl{int(time.time() * 1000)}_{abs(hash(url)) % 100000}"
        self.state = "pending"  # pending/running/paused/done/error
        self.error = ""
        self.total = 0
        self.done = 0
        self.speed = 0.0
        self.started_at = 0.0
        self.finished_at = 0.0

        self._stop = False
        self._lock = threading.Lock()
        self._segments: list[dict] = []
        self._threads: list[threading.Thread] = []
        self._final_url = url

    # ---------------- 路径 ----------------

    def _unique_name(self, name: str) -> str:
        self.dest_dir.mkdir(parents=True, exist_ok=True)
        base = Path(name).stem
        ext = Path(name).suffix
        candidate = Path(name)
        i = 1
        while (self.dest_dir / candidate).exists():
            candidate = Path(f"{base} ({i}){ext}")
            i += 1
        return candidate.name

    @property
    def target(self) -> Path:
        return self.dest_dir / self.filename

    @property
    def meta_path(self) -> Path:
        return self.dest_dir / (self.filename + ".vhmeta")

    @property
    def percent(self) -> float:
        if self.total <= 0:
            return 100.0 if self.state == "done" else 0.0
        return min(100.0, self.done * 100.0 / self.total)

    # ---------------- 状态 ----------------

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "id": self.id,
                "url": self.url,
                "filename": self.filename,
                "state": self.state,
                "error": self.error,
                "total": self.total,
                "done": self.done,
                "percent": round(self.percent, 1),
                "speed": round(self.speed, 1),
                "threads": self.threads,
                "segments": len(self._segments),
            }

    def _notify(self) -> None:
        if self.on_progress:
            try:
                self.on_progress(self)
            except Exception:
                pass

    # ---------------- 主流程 ----------------

    def start(self, wait: bool = False) -> None:
        if self.state == "running":
            return
        self._stop = False
        self.state = "running"
        self.started_at = time.time()
        t = threading.Thread(target=self._run, daemon=True, name=f"dl-{self.id}")
        t.start()
        if wait:
            t.join()

    def pause(self) -> None:
        self._stop = True
        self.state = "paused"

    def _run(self) -> None:
        try:
            info = probe(self.url, self.referer, self.user_agent)
            if not info.get("ok"):
                raise DownloadError(info.get("error") or "探测失败")

            self._final_url = info.get("url") or self.url
            self.total = int(info.get("size") or 0)
            use_range = bool(info.get("range")) and self.total > MIN_SEGMENT

            if not use_range:
                self._download_single()
            else:
                self._download_segments()

            if self._stop:
                self.state = "paused"
                return

            self._merge()
            self.finished_at = time.time()
            self.state = "done"
            self.done = self.total
            self._cleanup_meta()
            self._notify()
        except Exception as e:
            self.state = "error"
            self.error = str(e)
            self._notify()

    # ---------------- 单线程 ----------------

    def _download_single(self) -> None:
        part = self.dest_dir / (self.filename + ".part")
        offset = part.stat().st_size if part.exists() else 0
        headers = {
            "User-Agent": self.user_agent,
            "Accept": "*/*",
            "Accept-Encoding": "identity",
        }
        if self.referer:
            headers["Referer"] = self.referer
        if offset:
            headers["Range"] = f"bytes={offset}-"

        with _open(self._final_url, headers, timeout=30) as resp:
            if offset and getattr(resp, "status", 200) == 200:
                offset = 0
                part.unlink(missing_ok=True)

            mode = "ab" if offset else "wb"
            with open(part, mode) as f:
                last = time.time()
                last_done = offset
                while not self._stop:
                    chunk = resp.read(262144)
                    if not chunk:
                        break
                    f.write(chunk)
                    with self._lock:
                        self.done += len(chunk)
                    now = time.time()
                    if now - last >= 0.5:
                        with self._lock:
                            self.speed = (self.done - last_done) / (now - last)
                        last, last_done = now, self.done
                        self._notify()

        if self._stop:
            return
        self._segments = [{"path": str(part), "done": 1}]

    # ---------------- 分段 ----------------

    def _download_segments(self) -> None:
        n = self.threads
        size = self.total
        seg_size = max(MIN_SEGMENT, size // n)
        segs = []
        start = 0
        idx = 0
        while start < size:
            end = min(start + seg_size - 1, size - 1)
            path = self.dest_dir / f"{self.filename}.part{idx}"
            done = path.stat().st_size if path.exists() else 0
            # 已经完整的段直接跳过
            if done >= end - start + 1:
                done = end - start + 1
            segs.append({"i": idx, "start": start, "end": end, "path": str(path), "done": done})
            start = end + 1
            idx += 1

        with self._lock:
            self._segments = segs
            self.done = sum(s["done"] for s in segs)

        self._save_meta()
        self._notify()

        workers = []
        for seg in segs:
            if seg["done"] >= seg["end"] - seg["start"] + 1:
                continue
            t = threading.Thread(target=self._fetch_segment, args=(seg,), daemon=True)
            workers.append(t)
            t.start()

        for t in workers:
            t.join()

        if not self._stop:
            with self._lock:
                self.done = self.total

    def _fetch_segment(self, seg: dict) -> None:
        length = seg["end"] - seg["start"] + 1
        if seg["done"] >= length:
            return

        attempt = 0
        while attempt < 3 and not self._stop:
            attempt += 1
            headers = {
                "User-Agent": self.user_agent,
                "Accept": "*/*",
                "Accept-Encoding": "identity",
                "Range": f"bytes={seg['start'] + seg['done']}-{seg['end']}",
            }
            if self.referer:
                headers["Referer"] = self.referer
            try:
                with _open(self._final_url, headers, timeout=30) as resp:
                    if getattr(resp, "status", 200) != 206:
                        raise DownloadError("服务器未返回分段内容")
                    with open(seg["path"], "ab") as f:
                        last = time.time()
                        last_done = self.done
                        while not self._stop:
                            chunk = resp.read(262144)
                            if not chunk:
                                break
                            f.write(chunk)
                            with self._lock:
                                seg["done"] += len(chunk)
                                self.done += len(chunk)
                            now = time.time()
                            if now - last >= 0.5:
                                with self._lock:
                                    self.speed = (self.done - last_done) / (now - last)
                                last, last_done = now, self.done
                                self._notify()
                if seg["done"] >= length:
                    return
            except Exception:
                if self._stop:
                    return
                time.sleep(0.4 * attempt)

        if seg["done"] < length and not self._stop:
            raise DownloadError(f"第 {seg['i'] + 1} 段下载不完整")

    # ---------------- 合并 ----------------

    def _merge(self) -> None:
        if len(self._segments) == 1 and self._segments[0]["path"].endswith(".part"):
            src = Path(self._segments[0]["path"])
            if self.target.exists():
                self.target.unlink()
            shutil.move(str(src), str(self.target))
            return

        tmp = self.target.with_suffix(self.target.suffix + ".merging")
        with open(tmp, "wb") as out:
            for seg in sorted(self._segments, key=lambda s: s["i"]):
                p = Path(seg["path"])
                if not p.exists():
                    raise DownloadError(f"分段文件丢失：{p.name}")
                with open(p, "rb") as f:
                    shutil.copyfileobj(f, out, 1024 * 1024)
                p.unlink(missing_ok=True)

        if self.target.exists():
            self.target.unlink()
        shutil.move(str(tmp), str(self.target))

    # ---------------- meta ----------------

    def _save_meta(self) -> None:
        try:
            self.meta_path.write_text(
                json.dumps(
                    {
                        "url": self.url,
                        "total": self.total,
                        "segments": self._segments,
                        "filename": self.filename,
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
        except Exception:
            pass

    def _cleanup_meta(self) -> None:
        try:
            self.meta_path.unlink(missing_ok=True)
        except Exception:
            pass
