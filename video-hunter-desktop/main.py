"""Video Hunter Desktop 入口。

启动后做三件事：
1. 起本地 MITM 代理（默认 127.0.0.1:8899）
2. 起 Web 控制台（默认 127.0.0.1:8900）
3. 打开浏览器到控制台

命令行参数：
  --port 8899        代理端口
  --ui-port 8900     控制台端口
  --dest D:\\videos   下载目录
  --threads 8        每个任务的分段数
  --auto-proxy       启动时自动接管系统代理
  --no-browser       不自动打开浏览器
"""

from __future__ import annotations

import argparse
import sys
import threading
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

import systemproxy
from certs import CertManager
from downloader import DownloadTask
from hlsdl import HlsDownloader
from proxy import MediaHit, ProxyServer
from sniffer import filename_for, human_size
from store import AppState, load_config, save_config
from ui import open_browser, start_ui


class HlsTask:
    """把 HlsDownloader 包成和 DownloadTask 一样的接口，方便 UI 统一展示。"""

    def __init__(self, url: str, dest_dir: Path, filename: str, referer: str,
                 user_agent: str, threads: int, on_progress=None):
        self.id = f"hls{int(time.time() * 1000)}"
        self.url = url
        self.dest_dir = Path(dest_dir)
        self.filename = filename
        self.referer = referer
        self.user_agent = user_agent
        self.threads = threads
        self.on_progress = on_progress

        self.state = "pending"
        self.error = ""
        self.total = 0
        self.done = 0
        self.percent = 0.0
        self.speed = 0.0
        self._dl = HlsDownloader(
            url=url,
            dest_dir=self.dest_dir,
            filename=filename,
            referer=referer,
            user_agent=user_agent,
            threads=threads,
            on_progress=self._on_progress,
        )

    def _on_progress(self, info: dict) -> None:
        self.done = int(info.get("done") or 0)
        self.total = int(info.get("total") or 0)
        self.percent = float(info.get("percent") or 0)
        self.speed = float(info.get("speed") or 0) * 1024
        if self.on_progress:
            try:
                self.on_progress(self)
            except Exception:
                pass

    def snapshot(self) -> dict:
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
            "segments": self.total,
        }

    def start(self) -> None:
        self.state = "running"
        t = threading.Thread(target=self._run, daemon=True, name=f"hls-{self.id}")
        t.start()

    def _run(self) -> None:
        try:
            out = self._dl.download()
            self.filename = out.name
            self.state = "done"
            self.percent = 100.0
        except Exception as e:
            self.state = "error"
            self.error = str(e)

    def pause(self) -> None:
        self._dl.pause()
        self.state = "paused"


class App:
    def __init__(self, config: dict):
        self.config = config
        self.state = AppState(config)
        self.certs = CertManager(BASE)
        self.proxy = ProxyServer(
            certs=self.certs,
            on_media=self.on_media,
            host="127.0.0.1",
            port=int(config.get("proxy_port", 8899)),
        )
        self.state.proxy = self.proxy
        self._ui = None

    # ---------------- 代理回调 ----------------

    def on_media(self, hit: MediaHit) -> None:
        self.state.add_media(hit)

    # ---------------- 下载 ----------------

    def start_download(self, body: dict) -> dict:
        url = (body.get("url") or "").strip()
        if not url:
            return {"ok": False, "error": "缺少链接"}

        kind = body.get("kind") or "file"
        referer = body.get("referer") or ""
        filename = body.get("filename") or filename_for(url, kind)
        dest = Path(self.config["dest_dir"])
        threads = int(self.config.get("threads", 8))

        try:
            if kind == "hls":
                task = HlsTask(
                    url=url,
                    dest_dir=dest,
                    filename=filename,
                    referer=referer,
                    user_agent="",
                    threads=max(4, threads),
                    on_progress=self._noop,
                )
            elif kind == "dash":
                return {"ok": False, "error": "DASH(.mpd) 暂不支持，请用 yt-dlp"}
            else:
                task = DownloadTask(
                    url=url,
                    dest_dir=dest,
                    filename=filename,
                    referer=referer,
                    threads=threads,
                    on_progress=self._noop,
                )

            self.state.add_task(task)
            task.start()
            return {"ok": True, "id": task.id}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def _noop(self, _task) -> None:
        return

    def pause_task(self, task_id: str) -> dict:
        t = self.state.get_task(task_id)
        if not t:
            return {"ok": False, "error": "任务不存在"}
        t.pause()
        return {"ok": True}

    def resume_task(self, task_id: str) -> dict:
        t = self.state.get_task(task_id)
        if not t:
            return {"ok": False, "error": "任务不存在"}
        if t.state == "done":
            return {"ok": False, "error": "任务已完成"}
        t.start()
        return {"ok": True}

    # ---------------- 代理开关 ----------------

    def toggle_proxy(self, enable: bool) -> dict:
        if enable:
            ok, msg = systemproxy.enable("127.0.0.1", int(self.config.get("proxy_port", 8899)))
        else:
            ok, msg = systemproxy.disable()
        if ok:
            self.config["auto_proxy"] = enable
            save_config(BASE, self.config)
        return {"ok": ok, "message": msg}

    # ---------------- 快照 ----------------

    def snapshot(self) -> dict:
        snap = self.state.snapshot()
        snap["config"]["auto_proxy"] = bool(self.config.get("auto_proxy"))
        snap["config"]["proxy_running"] = self.proxy.running
        snap["config"]["port"] = int(self.config.get("proxy_port", 8899))
        return snap


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Video Hunter Desktop")
    p.add_argument("--port", type=int, default=8899, help="代理端口")
    p.add_argument("--ui-port", type=int, default=8900, help="控制台端口")
    p.add_argument("--dest", default="", help="下载目录")
    p.add_argument("--threads", type=int, default=8, help="分段线程数")
    p.add_argument("--auto-proxy", action="store_true", help="启动时接管系统代理")
    p.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])

    config = load_config(BASE)
    config["proxy_port"] = args.port
    config["ui_port"] = args.ui_port
    config["threads"] = args.threads
    if args.dest:
        config["dest_dir"] = str(Path(args.dest).expanduser().resolve())
        Path(config["dest_dir"]).mkdir(parents=True, exist_ok=True)
    if args.auto_proxy:
        config["auto_proxy"] = True
    save_config(BASE, config)

    try:
        app = App(config)
    except Exception as e:
        print(f"[错误] 初始化失败：{e}", file=sys.stderr)
        return 1

    try:
        app.proxy.start()
    except OSError as e:
        print(f"[错误] 代理端口 {config['proxy_port']} 启动失败：{e}", file=sys.stderr)
        print("       换个端口：python main.py --port 8898", file=sys.stderr)
        return 1

    ui_port = int(config.get("ui_port", 8900))
    try:
        app._ui = start_ui(app, "127.0.0.1", ui_port)
    except OSError as e:
        print(f"[错误] 控制台端口 {ui_port} 被占用：{e}", file=sys.stderr)
        app.proxy.stop()
        return 1

    print("=" * 58)
    print("  Video Hunter Desktop")
    print("=" * 58)
    print(f"  代理地址   http://127.0.0.1:{config['proxy_port']}")
    print(f"  控制台     http://127.0.0.1:{ui_port}/")
    print(f"  下载目录   {config['dest_dir']}")
    print(f"  根证书     {app.certs.ca_path}")
    print(f"  证书指纹   {app.certs.ca_fingerprint()}")
    print("=" * 58)

    if config.get("auto_proxy"):
        ok, msg = systemproxy.enable("127.0.0.1", int(config["proxy_port"]))
        print(f"  [系统代理] {msg}")

    if not args.no_browser:
        threading.Timer(0.8, lambda: open_browser(ui_port)).start()

    print("  按 Ctrl+C 退出\n")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n正在退出…")
    finally:
        app.proxy.stop()
        if config.get("auto_proxy"):
            systemproxy.disable()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
