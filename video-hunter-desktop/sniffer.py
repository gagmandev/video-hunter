"""媒体资源识别。

代理把每个响应头交给这里判断：是不是视频/音频、属于哪种协议。
规则和浏览器扩展里那套保持一致，另外补了桌面端常见的几种情况。
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit, unquote

HLS_RE = re.compile(r"\.m3u8?(?:[?#]|$)", re.I)
DASH_RE = re.compile(r"\.mpd(?:[?#]|$)", re.I)
SEGMENT_RE = re.compile(r"\.(ts|m4s|cmfv|cmfa|aac)(?:[?#]|$)", re.I)
FILE_RE = re.compile(r"\.(mp4|m4v|webm|mkv|mov|flv|f4v|ogv|avi|wmv|3gp)(?:[?#]|$)", re.I)
YT_RE = re.compile(r"googlevideo\.com/videoplayback", re.I)
BILI_RE = re.compile(r"(bilivideo\.com|mcdn\.bilibili\.cn|upgcxcode)", re.I)
MIME_PARAM_RE = re.compile(r"[?&]mime=(?:video|audio)(?:%2F|/)", re.I)

# 小于这个体积的直链基本是预览图/广告片头
MIN_SIZE = 100 * 1024
# 分片大小通常几十 KB 到几 MB，用来判断"这是一条分片流"
SEGMENT_HINT_SIZE = 8 * 1024 * 1024

PLAYLIST_MIMES = ("mpegurl", "dash+xml", "vnd.apple")
VIDEO_MIMES = ("video/",)
AUDIO_MIMES = ("audio/",)


def classify(url: str, mime: str = "", size: int = 0) -> str | None:
    """返回 hls / dash / file / segment / youtube / bilibili，不认识返回 None。"""
    u = (url or "").split("#")[0]
    m = (mime or "").lower()

    if YT_RE.search(u):
        return "youtube"
    if HLS_RE.search(u) or any(x in m for x in PLAYLIST_MIMES):
        return "hls"
    if DASH_RE.search(u):
        return "dash"
    if SEGMENT_RE.search(u):
        return "segment"
    if BILI_RE.search(u) and (m.startswith(VIDEO_MIMES) or m.startswith(AUDIO_MIMES)):
        return "bilibili"
    if FILE_RE.search(u) or m.startswith(VIDEO_MIMES) or m.startswith(AUDIO_MIMES):
        return "file"
    if MIME_PARAM_RE.search(u):
        return "file"
    return None


def is_candidate(kind: str | None, url: str, mime: str, size: int) -> bool:
    """过滤掉噪音：广告小文件、分片、纯音频封面等。"""
    if not kind:
        return False
    if kind == "segment":
        # 分片不单独列出，但大体积说明可能是伪装成 ts 的完整文件
        return size >= SEGMENT_HINT_SIZE
    if kind in ("hls", "dash"):
        return True
    if kind == "youtube":
        return True
    if kind in ("file", "bilibili"):
        if size and size < MIN_SIZE:
            return False
        # 音频也算，很多站视频是分离的
        return True
    return False


def dedupe_key(url: str, kind: str) -> str:
    """同一条流可能被请求很多次（range 分片），归并成一条。"""
    if kind == "youtube":
        m = re.search(r"[?&]itag=(\d+)", url)
        if m:
            return "yt:" + m.group(1)
    # 去掉明显的 range 参数，避免同一文件被拆成很多条
    u = re.sub(r"[?&](range|start|end|bytestart|byteend)=\d+", "", url)
    return u.split("#")[0]


def filename_for(url: str, kind: str, fallback: str = "video") -> str:
    """从 URL 猜一个像样的文件名。"""
    try:
        parts = urlsplit(url)
        seg = unquote(parts.path.rsplit("/", 1)[-1])
    except Exception:
        seg = ""

    seg = re.sub(r"\.(m3u8?|mpd|ts|m4s|mp4|webm|flv)$", "", seg, flags=re.I)
    generic = re.fullmatch(
        r"(index|playlist|master|main|video|media|hls|out|stream|manifest|chunklist|"
        r"file|videoplayback|.*\.m4s|.*\.ts)",
        seg or "",
        re.I,
    )

    if not seg or len(seg) < 2 or generic:
        seg = re.sub(r"[\\/:*?\"<>|\r\n\t]", "_", fallback).strip() or "video"

    seg = seg[:100]
    if kind == "hls":
        return seg + ".ts"
    if kind == "dash":
        return seg + ".mp4"
    return seg + ".mp4"


def human_size(n: int) -> str:
    if not n or n <= 0:
        return "未知"
    units = ["B", "KB", "MB", "GB", "TB"]
    v = float(n)
    i = 0
    while v >= 1024 and i < len(units) - 1:
        v /= 1024
        i += 1
    return f"{v:.0f} {units[i]}" if v >= 100 else f"{v:.1f} {units[i]}"
