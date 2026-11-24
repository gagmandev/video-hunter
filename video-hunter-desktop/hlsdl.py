"""HLS（m3u8）下载引擎。

支持：
- master playlist 多清晰度解析
- 分片并发下载
- AES-128 解密（含 fMP4 的 init segment）
- #EXT-X-BYTERANGE 字节范围分片
- DRM（SAMPLE-AES / skd://）明确报错，不产出坏文件
"""

from __future__ import annotations

import os
import re
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Callable
from urllib.parse import urljoin, urlsplit

from aes import cbc_decrypt

UA_DEFAULT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


class HlsError(RuntimeError):
    pass


class DrmError(HlsError):
    pass


def _headers(referer: str = "", ua: str = "") -> dict[str, str]:
    h = {
        "User-Agent": ua or UA_DEFAULT,
        "Accept": "*/*",
        "Accept-Encoding": "identity",
    }
    if referer:
        h["Referer"] = referer
    return h


def fetch_bytes(url: str, referer: str = "", ua: str = "", timeout: int = 30,
                byte_range: tuple[int, int] | None = None) -> bytes:
    h = _headers(referer, ua)
    if byte_range:
        h["Range"] = f"bytes={byte_range[0]}-{byte_range[1]}"
    req = urllib.request.Request(url, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def fetch_text(url: str, referer: str = "", ua: str = "", timeout: int = 30) -> str:
    return fetch_bytes(url, referer, ua, timeout).decode("utf-8", "replace")


def parse_attrs(text: str) -> dict[str, str]:
    out = {}
    i = 0
    n = len(text)
    while i < n:
        eq = text.find("=", i)
        if eq < 0:
            break
        key = text[i:eq].strip()
        j = eq + 1
        if j < n and text[j] == '"':
            end = text.find('"', j + 1)
            if end < 0:
                out[key] = text[j + 1:]
                break
            out[key] = text[j + 1:end]
            i = end + 2
            while i < n and text[i] in ", ":
                i += 1
        else:
            end = text.find(",", j)
            if end < 0:
                out[key] = text[j:].strip()
                break
            out[key] = text[j:end].strip()
            i = end + 1
    return out


def detect_drm(method: str, uri: str) -> str | None:
    m = (method or "").upper()
    if m.startswith("SAMPLE-AES"):
        return "SAMPLE-AES（Widevine / FairPlay 一类）"
    u = (uri or "").strip()
    if u.startswith("skd://"):
        return "FairPlay"
    if u.startswith("data:") or u.startswith("http://") or u.startswith("https://") or not u:
        return None
    return "未知密钥协议"


class HlsDownloader:
    def __init__(
        self,
        url: str,
        dest_dir: str | Path,
        filename: str,
        referer: str = "",
        user_agent: str = "",
        threads: int = 10,
        on_progress: Callable[[dict], None] | None = None,
    ):
        self.url = url
        self.dest_dir = Path(dest_dir)
        self.dest_dir.mkdir(parents=True, exist_ok=True)
        self.filename = filename
        self.referer = referer
        self.user_agent = user_agent or UA_DEFAULT
        self.threads = max(1, min(16, threads))
        self.on_progress = on_progress

        self._stop = False
        self._done = 0
        self._total = 0
        self._lock = threading.Lock()
        self._started = 0.0

    def pause(self) -> None:
        self._stop = True

    def _emit(self, **kw) -> None:
        if not self.on_progress:
            return
        payload = {
            "done": self._done,
            "total": self._total,
            "percent": (self._done * 100.0 / self._total) if self._total else 0.0,
            "speed": 0.0,
            **kw,
        }
        try:
            self.on_progress(payload)
        except Exception:
            pass

    # ---------------- 解析 ----------------

    def parse(self) -> dict:
        text = fetch_text(self.url, self.referer, self.user_agent)
        if "#EXT-X-STREAM-INF" in text:
            variants = self._parse_master(text, self.url)
            if not variants:
                raise HlsError("主播放列表里没有可用清晰度")
            preview = None
            try:
                preview = self._parse_media(
                    fetch_text(variants[0]["url"], self.referer, self.user_agent),
                    variants[0]["url"],
                )
            except Exception:
                pass
            return {
                "kind": "master",
                "variants": variants,
                "segments": len(preview["segments"]) if preview else 0,
                "duration": preview["duration"] if preview else 0,
            }

        info = self._parse_media(text, self.url)
        if info.get("drm"):
            raise DrmError("DRM 保护（" + info["drm"] + "），无法下载")
        return {
            "kind": "media",
            "variants": [],
            "segments": len(info["segments"]),
            "duration": info["duration"],
            "drm": info.get("drm"),
        }

    def _parse_master(self, text: str, base: str) -> list[dict]:
        lines = [l.strip() for l in text.splitlines()]
        variants = []
        for i, line in enumerate(lines):
            if not line.upper().startswith("#EXT-X-STREAM-INF"):
                continue
            attrs = parse_attrs(line.split(":", 1)[1] if ":" in line else "")
            j = i + 1
            while j < len(lines) and (not lines[j] or lines[j].startswith("#")):
                j += 1
            if j >= len(lines):
                break
            variants.append({
                "url": urljoin(base, lines[j]),
                "bandwidth": int(attrs.get("BANDWIDTH") or attrs.get("AVERAGE-BANDWIDTH") or 0),
                "resolution": attrs.get("RESOLUTION", ""),
                "codecs": attrs.get("CODECS", ""),
            })
        variants.sort(key=lambda v: v["bandwidth"], reverse=True)
        return variants

    def _parse_media(self, text: str, base: str) -> dict:
        lines = [l.strip() for l in text.splitlines()]

        key = None
        drm = None
        map_uri = None
        map_range = None
        media_seq = 0
        segments = []
        duration = 0.0
        pending_range = None

        for line in lines:
            if not line:
                continue

            if line.startswith("#EXT-X-MEDIA-SEQUENCE:"):
                try:
                    media_seq = int(line.split(":", 1)[1])
                except ValueError:
                    media_seq = 0
                continue

            if line.upper().startswith("#EXT-X-SESSION-KEY"):
                attrs = parse_attrs(line.split(":", 1)[1])
                d = detect_drm(attrs.get("METHOD", ""), attrs.get("URI", ""))
                if d:
                    drm = d
                continue

            if line.startswith("#EXT-X-KEY:"):
                attrs = parse_attrs(line.split(":", 1)[1])
                method = (attrs.get("METHOD") or "").upper()
                uri = attrs.get("URI", "")
                if method and method != "NONE":
                    d = detect_drm(method, urljoin(base, uri) if uri else "")
                    if d:
                        drm = d
                    key = {
                        "method": method,
                        "uri": urljoin(base, uri) if uri else "",
                        "iv": attrs.get("IV", ""),
                    }
                else:
                    key = None
                continue

            if line.startswith("#EXT-X-MAP:"):
                attrs = parse_attrs(line.split(":", 1)[1])
                if attrs.get("URI"):
                    map_uri = urljoin(base, attrs["URI"])
                    br = attrs.get("BYTERANGE")
                    if br and "@" in br:
                        ln, off = br.split("@", 1)
                        map_range = (int(off), int(off) + int(ln) - 1)
                continue

            if line.startswith("#EXT-X-BYTERANGE:"):
                v = line.split(":", 1)[1]
                if "@" in v:
                    ln, off = v.split("@", 1)
                    pending_range = (int(off), int(off) + int(ln) - 1)
                else:
                    ln = int(v)
                    pending_range = ("rel", ln)
                continue

            if line.startswith("#EXTINF:"):
                try:
                    duration += float(line.split(":", 1)[1].split(",")[0])
                except ValueError:
                    pass
                continue

            if line.startswith("#"):
                continue

            seg_url = urljoin(base, line)
            seg = {"url": seg_url, "range": None}
            if pending_range:
                if pending_range[0] == "rel" and segments:
                    prev_end = segments[-1].get("end", 0)
                    seg["range"] = (prev_end + 1, prev_end + pending_range[1])
                elif pending_range[0] != "rel":
                    seg["range"] = pending_range
                pending_range = None
            if seg["range"]:
                seg["end"] = seg["range"][1]
            segments.append(seg)

        return {
            "segments": segments,
            "key": key,
            "drm": drm,
            "map_uri": map_uri,
            "map_range": map_range,
            "media_seq": media_seq,
            "duration": duration,
            "is_fmp4": map_uri is not None,
        }

    # ---------------- 下载 ----------------

    def download(self, variant_url: str | None = None, out_name: str | None = None) -> Path:
        start_url = variant_url or self.url
        text = fetch_text(start_url, self.referer, self.user_agent)
        if "#EXT-X-STREAM-INF" in text:
            variants = self._parse_master(text, start_url)
            if not variants:
                raise HlsError("没有可用清晰度")
            start_url = variants[0]["url"]
            text = fetch_text(start_url, self.referer, self.user_agent)

        info = self._parse_media(text, start_url)
        if info.get("drm"):
            raise DrmError("DRM 保护（" + info["drm"] + "），无法下载")

        segments = info["segments"]
        if not segments:
            raise HlsError("播放列表里没有分片，可能是直播流或该格式不支持")

        self._total = len(segments) + (1 if info["map_uri"] else 0)
        self._done = 0
        self._started = time.time()

        # 密钥先取一次，避免每个分片重复请求
        key_bytes = None
        if info["key"]:
            key_bytes = fetch_bytes(info["key"]["uri"], self.referer, self.user_agent)
            if len(key_bytes) not in (16, 24, 32):
                raise HlsError(f"密钥长度异常：{len(key_bytes)} 字节")
            if len(key_bytes) != 16:
                raise HlsError("只支持 AES-128 密钥")

        out_name = out_name or self.filename
        out_path = self.dest_dir / out_name
        tmp_path = out_path.with_suffix(out_path.suffix + ".hls.tmp")

        results: dict[int, bytes] = {}

        # init segment（fMP4）
        if info["map_uri"]:
            try:
                init = fetch_bytes(
                    info["map_uri"], self.referer, self.user_agent, byte_range=info["map_range"]
                )
                if key_bytes and info["key"] and info["key"].get("method") == "AES-128":
                    init = self._decrypt(init, key_bytes, info["key"], info["media_seq"])
                results[-1] = init
            except Exception as e:
                raise HlsError("初始化分片下载失败：" + str(e))
            with self._lock:
                self._done += 1
            self._emit(label="初始化分片")

        def work(idx: int, seg: dict) -> tuple[int, bytes]:
            if self._stop:
                raise HlsError("已暂停")
            data = fetch_bytes(
                seg["url"], self.referer, self.user_agent, byte_range=seg.get("range")
            )
            if key_bytes and info["key"] and info["key"].get("method") == "AES-128":
                data = self._decrypt(data, key_bytes, info["key"], info["media_seq"] + idx)
            with self._lock:
                self._done += 1
                done = self._done
                speed = done / max(0.001, time.time() - self._started)
            self._emit(done=done, total=self._total, speed=speed, label=f"分片 {done}/{self._total}")
            return idx, data

        with ThreadPoolExecutor(max_workers=self.threads) as pool:
            futures = [pool.submit(work, i, s) for i, s in enumerate(segments)]
            for f in as_completed(futures):
                idx, data = f.result()
                results[idx] = data

        if self._stop:
            raise HlsError("已暂停")

        with open(tmp_path, "wb") as out:
            if -1 in results:
                out.write(results[-1])
            for i in range(len(segments)):
                out.write(results[i])

        if out_path.exists():
            out_path.unlink()
        tmp_path.replace(out_path)
        self._emit(done=self._total, total=self._total, percent=100.0, label="完成")
        return out_path

    @staticmethod
    def _decrypt(data: bytes, key: bytes, key_info: dict, seq: int) -> bytes:
        iv_hex = (key_info.get("iv") or "").strip()
        if iv_hex:
            iv_hex = iv_hex[2:] if iv_hex.lower().startswith("0x") else iv_hex
            try:
                iv = bytes.fromhex(iv_hex)
            except ValueError:
                iv = seq.to_bytes(16, "big")
        else:
            iv = seq.to_bytes(16, "big")
        return cbc_decrypt(key, iv, data)
