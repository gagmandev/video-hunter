"""本地 Web 控制台。

用内置 http.server 提供页面和 API，浏览器打开就是一个桌面应用界面。
这样做的好处是不依赖 tkinter / PyQt，任何装了 Python 的机器都能跑。
"""

from __future__ import annotations

import json
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

import systemproxy
from downloader import DownloadTask
from hlsdl import HlsDownloader
from sniffer import filename_for

PAGE = r'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Video Hunter Desktop</title>
<style>
:root{
  --bg:#f4f5f9; --panel:#fff; --text:#1b1c22; --muted:#7c8090; --line:#e6e8f0;
  --brand:#6c5ce7; --brand-soft:#efecfe; --ok:#00a884; --danger:#d63031;
  --shadow:0 1px 2px rgba(20,22,40,.05),0 6px 20px rgba(20,22,40,.06);
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;-webkit-font-smoothing:antialiased}
.wrap{max-width:1080px;margin:0 auto;padding:22px 20px 60px}
header{display:flex;align-items:center;gap:14px;margin-bottom:20px;flex-wrap:wrap}
.logo{width:38px;height:38px;border-radius:11px;background:linear-gradient(135deg,#7c6cf0,#5a4bd6);display:flex;align-items:center;justify-content:center;box-shadow:0 4px 14px rgba(108,92,231,.32)}
.logo svg{width:20px;height:20px}
.title h1{margin:0;font-size:17px;font-weight:650;letter-spacing:.2px}
.title p{margin:2px 0 0;font-size:12px;color:var(--muted)}
.spacer{flex:1}
.pill{display:inline-flex;align-items:center;gap:6px;font-size:12px;padding:5px 11px;border-radius:20px;background:var(--panel);border:1px solid var(--line);color:var(--muted)}
.dot{width:7px;height:7px;border-radius:50%;background:var(--ok);box-shadow:0 0 0 3px rgba(0,168,132,.14)}
.dot.off{background:#b6bac6;box-shadow:none}
button{font-family:inherit;cursor:pointer}
.btn{border:1px solid var(--line);background:var(--panel);color:var(--text);font-size:13px;font-weight:550;padding:8px 14px;border-radius:9px;transition:.15s}
.btn:hover{border-color:var(--brand);color:var(--brand)}
.btn.primary{background:var(--brand);border-color:var(--brand);color:#fff}
.btn.primary:hover{filter:brightness(1.08);color:#fff}
.btn.on{background:var(--ok);border-color:var(--ok);color:#fff}
.btn.on:hover{color:#fff;filter:brightness(1.05)}
.btn.small{padding:5px 11px;font-size:12px;border-radius:7px}
.tabs{display:flex;gap:6px;margin-bottom:14px;border-bottom:1px solid var(--line);padding-bottom:0}
.tab{background:none;border:none;padding:10px 4px;margin-right:18px;font-size:13.5px;font-weight:550;color:var(--muted);border-bottom:2px solid transparent;display:flex;align-items:center;gap:7px}
.tab.active{color:var(--brand);border-bottom-color:var(--brand)}
.count{background:var(--brand-soft);color:var(--brand);font-size:11px;font-weight:700;padding:1px 7px;border-radius:10px;min-width:20px;text-align:center}
.list{display:flex;flex-direction:column;gap:10px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:13px;padding:14px 16px;box-shadow:var(--shadow)}
.row{display:flex;align-items:center;gap:10px;min-width:0}
.badge{flex:0 0 auto;font-size:10px;font-weight:700;letter-spacing:.4px;padding:3px 7px;border-radius:6px;background:var(--brand-soft);color:var(--brand)}
.badge.hls{background:#eaf6ff;color:#0984c7}
.badge.dash{background:#fff1e6;color:#d97a2b}
.badge.file{background:#e8f8f2;color:#0a8f6f}
.badge.youtube{background:#ffecec;color:#c92a2a}
.badge.bilibili{background:#e6f7fe;color:#1a9fd4}
.name{font-size:13.5px;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;flex:1;min-width:0}
.meta{font-size:12px;color:var(--muted);margin-top:6px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.actions{display:flex;gap:8px;flex:0 0 auto}
.empty{text-align:center;padding:56px 20px;color:var(--muted)}
.empty strong{display:block;color:var(--text);font-size:15px;margin-bottom:8px}
.empty span{font-size:12.5px;line-height:1.8}
.track{height:6px;border-radius:4px;background:var(--line);overflow:hidden;margin-top:10px}
.bar{height:100%;width:0;border-radius:4px;background:linear-gradient(90deg,#6c5ce7,#a29bfe);transition:width .3s}
.bar.done{background:linear-gradient(90deg,#00a884,#4ecdc4)}
.bar.err{background:var(--danger)}
.task-foot{display:flex;align-items:center;justify-content:space-between;margin-top:7px;font-size:12px;color:var(--muted)}
.mono{font-variant-numeric:tabular-nums}
.toast{position:fixed;left:50%;bottom:28px;transform:translate(-50%,14px);background:#1b1c22;color:#fff;font-size:12.5px;padding:9px 18px;border-radius:9px;opacity:0;pointer-events:none;transition:.22s;z-index:99;max-width:70vw;text-align:center}
.toast.show{opacity:1;transform:translate(-50%,0)}
.help{margin-top:26px;font-size:12px;color:var(--muted);line-height:1.9;background:var(--panel);border:1px dashed var(--line);border-radius:12px;padding:14px 18px}
.help b{color:var(--text)}
code{background:#f0f1f6;padding:1px 5px;border-radius:4px;font-size:11.5px}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <div class="logo"><svg viewBox="0 0 24 24" fill="none" stroke="#fff" stroke-width="2" stroke-linecap="round"><path d="M8 5v14l11-7z" fill="#fff" stroke="none"/></svg></div>
    <div class="title">
      <h1>Video Hunter Desktop</h1>
      <p id="sub">本地代理 · 多线程下载</p>
    </div>
    <div class="spacer"></div>
    <div class="pill"><span class="dot" id="dot"></span><span id="proxyState">代理运行中</span></div>
    <button class="btn" id="sysProxy">开启系统代理</button>
  </header>

  <nav class="tabs">
    <button class="tab active" data-tab="media">捕获到的视频 <span class="count" id="mediaCount">0</span></button>
    <button class="tab" data-tab="tasks">下载任务 <span class="count" id="taskCount">0</span></button>
  </nav>

  <section id="pane-media"><div class="list" id="mediaList"></div></section>
  <section id="pane-tasks" hidden>
    <div class="list" id="taskList"></div>
    <div style="margin-top:14px;text-align:right"><button class="btn small" id="clearDone">清除已完成</button></div>
  </section>

  <div class="help">
    <b>怎么用：</b>点「开启系统代理」→ 在浏览器里打开视频页并播放 → 这里会自动列出流地址 → 点下载。<br>
    <b>没抓到？</b>浏览器如果开了 QUIC/HTTP3 会绕过代理，在 <code>chrome://flags</code> 里把 <code>Experimental QUIC protocol</code> 设为 Disabled。<br>
    <b>HTTPS 报证书错误？</b>需要先安装本地根证书，见 README 的「安装证书」一节。
  </div>
</div>
<div class="toast" id="toast"></div>

<script>
const $ = (s) => document.querySelector(s);
const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
let state = {media: [], tasks: [], config: {}, stats: {}};
let tab = 'media';
let busy = new Set();

function toast(msg) {
  const el = $('#toast');
  el.textContent = msg;
  el.classList.add('show');
  clearTimeout(el._t);
  el._t = setTimeout(() => el.classList.remove('show'), 2400);
}

function fmt(n) {
  if (!n || n <= 0) return '未知';
  const u = ['B','KB','MB','GB','TB'];
  let v = n, i = 0;
  while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
  return (v >= 100 ? v.toFixed(0) : v.toFixed(1)) + ' ' + u[i];
}

function kindLabel(k) {
  return {hls:'HLS', dash:'DASH', file:'直链', youtube:'YT', bilibili:'BILI', segment:'分片'}[k] || 'MEDIA';
}

function renderMedia() {
  const list = $('#mediaList');
  $('#mediaCount').textContent = state.media.length;
  if (!state.media.length) {
    list.innerHTML = `<div class="empty"><strong>还没有捕获到视频</strong>
      <span>确认系统代理已开启，然后在浏览器里播放视频。<br>流地址通常会在开始播放后 1-3 秒内出现。</span></div>`;
    return;
  }
  list.innerHTML = state.media.map(m => {
    const isHls = m.kind === 'hls';
    return `<div class="card">
      <div class="row">
        <span class="badge ${esc(m.kind)}">${kindLabel(m.kind)}</span>
        <span class="name" title="${esc(m.url)}">${esc(m.filename)}</span>
        <div class="actions">
          <button class="btn small" data-copy="${esc(m.id)}">复制链接</button>
          <button class="btn small primary" data-dl="${esc(m.id)}">下载</button>
        </div>
      </div>
      <div class="meta" title="${esc(m.url)}">${esc(m.size_text)} · ${esc(m.origin)}${m.hits > 1 ? ' · 命中 ' + m.hits + ' 次' : ''}</div>
    </div>`;
  }).join('');
}

function renderTasks() {
  const list = $('#taskList');
  $('#taskCount').textContent = state.tasks.length;
  if (!state.tasks.length) {
    list.innerHTML = `<div class="empty"><strong>没有下载任务</strong><span>在「捕获到的视频」里点下载。</span></div>`;
    return;
  }
  list.innerHTML = state.tasks.map(t => {
    const cls = t.state === 'done' ? 'done' : t.state === 'error' ? 'err' : '';
    const st = {running:'下载中', paused:'已暂停', done:'已完成', error:'失败', pending:'等待中'}[t.state] || t.state;
    const sp = t.state === 'running' && t.speed ? fmt(t.speed) + '/s' : '';
    const canPause = t.state === 'running';
    const canResume = t.state === 'paused' || t.state === 'error';
    return `<div class="card">
      <div class="row">
        <span class="name" title="${esc(t.filename)}">${esc(t.filename)}</span>
        <div class="actions">
          ${canPause ? `<button class="btn small" data-pause="${esc(t.id)}">暂停</button>` : ''}
          ${canResume ? `<button class="btn small" data-resume="${esc(t.id)}">继续</button>` : ''}
        </div>
      </div>
      <div class="track"><div class="bar ${cls}" style="width:${Math.max(2, t.percent)}%"></div></div>
      <div class="task-foot">
        <span>${st}${t.error ? ' · ' + esc(t.error) : ''}</span>
        <span class="mono">${fmt(t.done)} / ${fmt(t.total)} · ${t.percent.toFixed(0)}%${sp ? ' · ' + sp : ''}</span>
      </div>
    </div>`;
  }).join('');
}

function render() {
  renderMedia();
  renderTasks();
  const s = state.stats || {};
  $('#sub').textContent = `已处理 ${s.requests || 0} 个请求 · 命中 ${s.media || 0} 个媒体`;
}

async function poll() {
  try {
    const r = await fetch('/api/state');
    state = await r.json();
    render();
  } catch (e) { /* 后台可能正在重启 */ }
}

async function api(path, body) {
  const r = await fetch(path, {
    method: body ? 'POST' : 'GET',
    headers: {'Content-Type': 'application/json'},
    body: body ? JSON.stringify(body) : undefined
  });
  return await r.json();
}

document.addEventListener('click', async (e) => {
  const dl = e.target.closest('[data-dl]');
  if (dl) {
    const m = state.media.find(x => x.id === dl.dataset.dl);
    if (!m) return;
    dl.disabled = true;
    const res = await api('/api/download', {
      url: m.url, kind: m.kind, filename: m.filename, referer: m.referer
    });
    dl.disabled = false;
    if (res.ok) { toast('已开始下载'); switchTab('tasks'); }
    else toast(res.error || '启动失败');
    return;
  }

  const cp = e.target.closest('[data-copy]');
  if (cp) {
    const m = state.media.find(x => x.id === cp.dataset.copy);
    if (!m) return;
    try { await navigator.clipboard.writeText(m.url); toast('链接已复制'); }
    catch { toast('复制失败，请手动选择'); }
    return;
  }

  const pz = e.target.closest('[data-pause]');
  if (pz) { await api('/api/pause', {id: pz.dataset.pause}); return; }

  const rs = e.target.closest('[data-resume]');
  if (rs) { await api('/api/resume', {id: rs.dataset.resume}); return; }
});

document.querySelectorAll('.tab').forEach(b => {
  b.addEventListener('click', () => switchTab(b.dataset.tab));
});

function switchTab(name) {
  tab = name;
  document.querySelectorAll('.tab').forEach(b => b.classList.toggle('active', b.dataset.tab === name));
  $('#pane-media').hidden = name !== 'media';
  $('#pane-tasks').hidden = name !== 'tasks';
}

$('#sysProxy').addEventListener('click', async () => {
  const on = !state.config.auto_proxy;
  const res = await api('/api/proxy', {enable: on});
  if (res.ok) {
    toast(res.message);
    await poll();
    updateProxyBtn();
  } else {
    toast(res.message || '操作失败');
  }
});

function updateProxyBtn() {
  const btn = $('#sysProxy');
  const on = !!(state.config && state.config.auto_proxy);
  btn.classList.toggle('on', on);
  btn.textContent = on ? '系统代理已开启' : '开启系统代理';
  const dot = $('#dot');
  dot.classList.toggle('off', !(state.config && state.config.proxy_running));
  $('#proxyState').textContent = state.config && state.config.proxy_running
    ? `代理运行中 :${state.config.port || ''}` : '代理未运行';
}

$('#clearDone').addEventListener('click', async () => {
  await api('/api/clear-finished', {});
  poll();
});

poll();
setInterval(poll, 1200);
setInterval(updateProxyBtn, 1500);
</script>
</body>
</html>'''


class Handler(BaseHTTPRequestHandler):
    server_version = "VideoHunterUI/1.0"

    # 由 main 注入
    app = None  # type: ignore

    def log_message(self, fmt, *args):
        pass

    def _json(self, obj, code: int = 200):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _read_json(self) -> dict:
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if n <= 0:
                return {}
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return {}

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            data = PAGE.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if path == "/api/state":
            self._json(self.app.snapshot())
            return
        self._json({"ok": False, "error": "not found"}, 404)

    def do_POST(self):
        path = urlparse(self.path).path
        body = self._read_json()

        if path == "/api/download":
            self._json(self.app.start_download(body))
            return
        if path == "/api/pause":
            self._json(self.app.pause_task(body.get("id", "")))
            return
        if path == "/api/resume":
            self._json(self.app.resume_task(body.get("id", "")))
            return
        if path == "/api/clear-finished":
            self.app.state.clear_finished()
            self._json({"ok": True})
            return
        if path == "/api/proxy":
            self._json(self.app.toggle_proxy(bool(body.get("enable"))))
            return

        self._json({"ok": False, "error": "not found"}, 404)


def start_ui(app, host: str = "127.0.0.1", port: int = 8900) -> ThreadingHTTPServer:
    handler = type("BoundHandler", (Handler,), {"app": app})
    httpd = ThreadingHTTPServer((host, port), handler)
    t = threading.Thread(target=httpd.serve_forever, daemon=True, name="ui")
    t.start()
    return httpd


def open_browser(port: int) -> None:
    try:
        webbrowser.open(f"http://127.0.0.1:{port}/")
    except Exception:
        pass
