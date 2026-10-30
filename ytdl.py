# -*- coding: utf-8 -*-
"""
视频下载助手 —— 基于 yt-dlp，自动准备运行环境。

为什么 YouTube 不能直接用浏览器扩展下：
  它的视频轨和音频轨是分开的两条流（DASH），
  扩展只能拿到其中一条，拼不出完整文件。

用法：
  双击 run.bat，或命令行运行 python ytdl.py
"""

import os
import shutil
import subprocess
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
EXE = os.path.join(HERE, "yt-dlp.exe")
OUT_DIR = os.path.join(HERE, "downloads")

MIRRORS = [
    "https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp.exe",
    "https://ghproxy.net/https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp.exe",
    "https://gh-proxy.com/https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp.exe",
]

if sys.platform == "win32":
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def ensure_ytdlp():
    if os.path.exists(EXE) and os.path.getsize(EXE) > 1_000_000:
        return True

    print("首次使用，正在下载 yt-dlp.exe（约 17 MB）...")
    for url in MIRRORS:
        host = url.split("/")[2]
        try:
            print(f"  尝试 {host} ...")
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = resp.read()
            if len(data) > 1_000_000:
                with open(EXE, "wb") as f:
                    f.write(data)
                print(f"  完成，已保存到 {EXE}")
                return True
            print("  文件太小，可能不是有效程序")
        except Exception as e:
            print(f"  失败：{e}")

    print("\n自动下载失败。请手动下载后放到本目录：")
    print("  https://github.com/yt-dlp/yt-dlp/releases/latest")
    print(f"  目标路径：{EXE}")
    return False


def has_ffmpeg():
    if shutil.which("ffmpeg"):
        return True
    return os.path.exists(os.path.join(HERE, "ffmpeg.exe"))


def download(urls, with_ffmpeg):
    os.makedirs(OUT_DIR, exist_ok=True)

    if with_ffmpeg:
        fmt = "bv*+ba/b"
    else:
        fmt = "b[ext=mp4]/b"

    cmd = [
        EXE,
        "--no-playlist",
        "--no-overwrites",
        "-f", fmt,
        "-o", os.path.join(OUT_DIR, "%(title).80s.%(ext)s"),
    ]
    if with_ffmpeg:
        cmd += ["--merge-output-format", "mp4"]
    cmd += urls

    try:
        subprocess.run(cmd, check=False)
    except KeyboardInterrupt:
        print("\n已取消")


def main():
    print("=" * 46)
    print("  视频下载助手")
    print("=" * 46)

    if not ensure_ytdlp():
        input("\n按回车退出...")
        return

    ff = has_ffmpeg()
    if not ff:
        print("\n未检测到 ffmpeg，将下载已合并的单个文件（通常最高 720p）。")
        print("想要 4K / 单独音轨合并，请把 ffmpeg.exe 放到本目录。")

    print("\n粘贴链接，支持多行（直接回车退出）：")
    lines = []
    while True:
        try:
            line = input("> " if not lines else "  ").strip().strip('"').strip("'")
        except (EOFError, KeyboardInterrupt):
            break
        if not line:
            break
        if line.startswith("http"):
            lines.append(line)
        else:
            print("  看起来不是链接，已跳过")

    if not lines:
        print("没有输入链接，退出。")
        return

    print(f"\n开始下载 {len(lines)} 个视频...\n")
    download(lines, ff)

    print(f"\n完成。文件在：{OUT_DIR}")
    if not ff:
        print("提示：装个 ffmpeg 能拿到更高画质，也能只下音频。")
    input("\n按回车退出...")


if __name__ == "__main__":
    main()
