'use strict';

/* ============================================================
 * popup 界面逻辑
 * ============================================================ */

const $ = (sel) => document.querySelector(sel);

let currentTab = null;
let items = [];
let pageInfo = null;
const tasks = new Map(); // taskId -> itemId

/* ---------------- 基础工具 ---------------- */

function esc(str) {
  return String(str == null ? '' : str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

function send(msg) {
  return new Promise((resolve) => {
    chrome.runtime.sendMessage({ ...msg, target: 'background' }, (res) => {
      if (chrome.runtime.lastError) {
        resolve({ ok: false, error: chrome.runtime.lastError.message });
      } else {
        resolve(res || { ok: false, error: '后台无响应' });
      }
    });
  });
}

function fmtSize(n) {
  if (!n || n <= 0) return '大小未知';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let i = 0;
  let v = n;
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024;
    i++;
  }
  return (v >= 100 ? v.toFixed(0) : v.toFixed(1)) + ' ' + units[i];
}

const GENERIC = /^(index|playlist|master|main|video|media|hls|out|stream|manifest|chunklist|file|videoplayback)$/i;

/** YouTube 的 itag 对应画质，用来生成有意义的文件名 */
const YT_ITAG = {
  137: '1080p', 248: '1080p', 299: '1080p60',
  136: '720p', 247: '720p', 298: '720p60',
  135: '480p', 244: '480p',
  134: '360p', 243: '360p',
  133: '240p', 242: '240p',
  160: '144p', 278: '144p',
  140: '音频', 251: '音频', 139: '音频', 249: '音频',
  22: '720p', 18: '360p'
};

function guessFilename(item) {
  if (item.kind === 'bilibili-dash' || item.kind === 'bilibili-direct' || item.kind === 'bilibili-audio') {
    const title = (item.meta && item.meta.title) || (pageInfo && pageInfo.title) || (currentTab && currentTab.title) || 'bilibili';
    const clean = String(title).replace(/\s*-\s*bilibili\s*$/i, '').replace(/[\\/:*?"<>|]/g, '_').trim();
    if (item.kind === 'bilibili-dash') {
      const res = item.meta && item.meta.height ? `${item.meta.height}p` : 'dash';
      return `[${res}] ${clean.slice(0, 60)}.mp4`;
    }
    if (item.kind === 'bilibili-audio') {
      return `[${item.meta.label || 'audio'}] ${clean.slice(0, 60)}.m4a`;
    }
    return `${clean.slice(0, 70)}.flv`;
  }

  if (item.kind === 'youtube') {
    const m = String(item.url).match(/[?&]itag=(\d+)/);
    const tag = m ? YT_ITAG[m[1]] || 'itag' + m[1] : '';
    let title = (pageInfo && pageInfo.title) || (currentTab && currentTab.title) || 'youtube';
    title = title.replace(/\s*-\s*YouTube\s*$/i, '').replace(/[\\/:*?"<>|]/g, '_').trim();
    // 画质标签放最前面，标题被截断时也能分清是哪条流
    return tag ? `[${tag}] ${title.slice(0, 60)}.mp4` : `${title.slice(0, 70)}.mp4`;
  }

  let base = '';
  try {
    const u = new URL(item.url);
    const seg = u.pathname.split('/').filter(Boolean).pop() || '';
    base = decodeURIComponent(seg).replace(/\.(m3u8?|mpd|ts|m4s|mp4)$/i, '');
  } catch (_) { /* ignore */ }

  if (!base || base.length < 2 || GENERIC.test(base)) {
    const title = (pageInfo && pageInfo.title) || (currentTab && currentTab.title) || 'video';
    base = String(title).replace(/[\\/:*?"<>|]/g, '_').trim().slice(0, 80);
  }
  if (!base) base = 'video';
  return base + '.mp4';
}

/* ---------------- 渲染 ---------------- */

const ICON_EMPTY = `<svg viewBox="0 0 24 24" width="40" height="40" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
  <rect x="2" y="7" width="20" height="13" rx="2.5"/><path d="M8 3l4 4 4-4"/></svg>`;

function emptyHtml() {
  const pageUrl = (currentTab && currentTab.url) || '';
  if (/(^|\.)youtube\.com/i.test(pageUrl)) {
    return `<div class="empty">${ICON_EMPTY}
      <strong>YouTube 的流还没抓到</strong>
      <span>YouTube 用的是 DASH，视频和音频是两条独立的流。<br>
      先点播放，等 3 秒再点右上角刷新。<br>
      即使抓到了，也需要用项目里的 ytdl.py 合并音视频。</span>
    </div>`;
  }

  if (/(^|\.)bilibili\.com/i.test(pageUrl)) {
    return `<div class="empty">${ICON_EMPTY}
      <strong>B 站视频信息还没读取到</strong>
      <span>B 站的播放信息通过页面内嵌脚本注入。<br>
      先点播放，等 1–2 秒后点右上角刷新；<br>
      如果仍为空，可能是登录/地区/大会员限制导致无可用流。</span>
    </div>`;
  }

  const hasVideo = pageInfo && pageInfo.videos && pageInfo.videos.length > 0;
  if (hasVideo) {
    const n = pageInfo.videos.length;
    return `<div class="empty">${ICON_EMPTY}
      <strong>检测到播放器，还没抓到视频流</strong>
      <span>页面里有 ${n} 个 &lt;video&gt; 元素。点一下播放让它加载，再点右上角刷新。<br>
      如果地址是 blob: 开头，说明走的是 MSE 分片播放，等一会儿通常能抓到 m3u8。</span>
    </div>`;
  }
  return `<div class="empty">${ICON_EMPTY}
    <strong>当前页面没有发现视频</strong>
    <span>先播放一下页面里的视频，再点右上角刷新；<br>也可以在下方直接粘贴视频链接下载。</span>
  </div>`;
}

function hostOf(url) {
  try {
    return new URL(url).hostname;
  } catch (_) {
    return '';
  }
}

function cardHtml(item, name) {
  const isYt = item.kind === 'youtube';
  const isBiliDash = item.kind === 'bilibili-dash';
  const isBiliDirect = item.kind === 'bilibili-direct';
  const isBiliAudio = item.kind === 'bilibili-audio';
  const label = isYt ? 'YT' : isBiliDash || isBiliDirect || isBiliAudio ? 'BILI' : item.kind === 'hls' ? 'HLS' : item.kind === 'dash' ? 'DASH' : 'MP4';
  const cls = isYt ? 'youtube' : isBiliDash || isBiliDirect || isBiliAudio ? 'bilibili' : item.kind === 'file' ? 'file' : item.kind;
  const size = item.size ? fmtSize(item.size) : '大小未知';
  const kindText = isYt
    ? '音视频分离'
    : isBiliDash
      ? '音视频分离'
      : isBiliAudio
        ? '纯音频'
        : item.kind === 'hls'
          ? '分片流'
          : item.kind === 'dash'
            ? '需外部工具'
            : '直链';
  const meta = [size, hostOf(item.url), kindText].filter(Boolean).join(' · ');

  const foot = isYt
    ? `<button class="btn ghost" data-act="copy">复制链接</button>`
    : `<button class="btn ghost" data-act="copy">复制链接</button>
       <button class="btn primary" data-act="download">下载</button>`;

  return `<div class="card" data-id="${esc(item.id)}">
    <div class="card-head">
      <span class="badge ${esc(cls)}">${label}</span>
      <span class="name" title="${esc(name)}">${esc(name)}</span>
    </div>
    <div class="meta" title="${esc(item.url)}">${esc(meta)}</div>
    <div class="card-foot">${foot}</div>
    <div class="progress">
      <div class="track"><div class="bar"></div></div>
      <span class="ptext">准备中…</span>
    </div>
  </div>`;
}

function ytQuality(url) {
  const m = String(url).match(/[?&]itag=(\d+)/);
  return m ? YT_ITAG[m[1]] || '' : '';
}

/** 把 YouTube 的流分成画面和声音两组，各自按体积降序 */
function ytStreams() {
  const yt = items.filter((x) => x.kind === 'youtube');
  const isVideo = (x) =>
    /video/i.test(x.mime || '') || /[?&]mime=video/i.test(x.url);
  const isAudio = (x) =>
    /audio/i.test(x.mime || '') || /[?&]mime=audio/i.test(x.url);

  const videos = yt.filter(isVideo).sort((a, b) => (b.size || 0) - (a.size || 0));
  const audios = yt.filter(isAudio).sort((a, b) => (b.size || 0) - (a.size || 0));
  return { videos, audios };
}

function ytBannerHtml() {
  const { videos, audios } = ytStreams();
  if (!videos.length) return '';

  const v = videos[0];
  const a = audios[0];
  const q = ytQuality(v.url);
  const total = (v.size || 0) + (a ? a.size || 0 : 0);
  const ready = !!a;

  return `<div class="card yt-card" data-id="yt-full">
    <div class="card-head">
      <span class="badge youtube">YT</span>
      <span class="name">下载完整视频${q ? '（' + q + '）' : ''}</span>
    </div>
    <div class="meta">${
      ready
        ? '画面 + 声音合并成一个 mp4' + (total ? ' · 约 ' + fmtSize(total) : '')
        : '只抓到画面，声音轨还没出现 —— 播放几秒再刷新'
    }</div>
    <div class="card-foot">
      <button class="btn primary" data-act="yt-full"${ready ? '' : ' disabled'}>合并下载</button>
    </div>
    <div class="progress">
      <div class="track"><div class="bar"></div></div>
      <span class="ptext">准备中…</span>
    </div>
  </div>`;
}

function biliStreams() {
  const dash = items.filter((x) => x.kind === 'bilibili-dash').sort((a, b) => (b.meta.height || 0) - (a.meta.height || 0));
  const directs = items.filter((x) => x.kind === 'bilibili-direct');
  return { dash, directs };
}

function biliBannerHtml() {
  const { dash, directs } = biliStreams();
  if (!dash.length && !directs.length) return '';

  // 有直链优先展示直链（FLV 是完整文件）
  if (directs.length) {
    const d = directs[0];
    return `<div class="card bili-card" data-id="${esc(d.id)}">
      <div class="card-head">
        <span class="badge bilibili">BILI</span>
        <span class="name">下载完整视频（FLV 直链）</span>
      </div>
      <div class="meta">${d.size ? fmtSize(d.size) : '大小未知'} · 单文件，无需合并</div>
      <div class="card-foot">
        <button class="btn primary" data-act="download">下载</button>
      </div>
      <div class="progress">
        <div class="track"><div class="bar"></div></div>
        <span class="ptext">准备中…</span>
      </div>
    </div>`;
  }

  const v = dash[0];
  const ready = !!v.meta.audioUrl;
  const total = v.size || 0;

  return `<div class="card bili-card" data-id="bili-full">
    <div class="card-head">
      <span class="badge bilibili">BILI</span>
      <span class="name">下载完整视频（${v.meta.height || '?'}p）</span>
    </div>
    <div class="meta">${
      ready
        ? '画面 + 声音合并成一个 mp4' + (total ? ' · 约 ' + fmtSize(total) : '')
        : '只有画面，没有对应音轨 —— 播放几秒后刷新'
    }</div>
    <div class="card-foot">
      <button class="btn primary" data-act="bili-full"${ready ? '' : ' disabled'}>合并下载</button>
    </div>
    <div class="progress">
      <div class="track"><div class="bar"></div></div>
      <span class="ptext">准备中…</span>
    </div>
  </div>`;
}

/** 同名文件自动加序号，避免多个 HLS 都叫「页面标题.mp4」 */
function uniqueNames(list) {
  const seen = new Map();
  return list.map((item) => {
    const name = guessFilename(item);
    const n = seen.get(name) || 0;
    seen.set(name, n + 1);
    if (n === 0) return name;
    const dot = name.lastIndexOf('.');
    return name.slice(0, dot) + ` (${n + 1})` + name.slice(dot);
  });
}

function render() {
  const list = $('#list');
  if (!items.length) {
    list.innerHTML = emptyHtml();
  } else {
    const names = uniqueNames(items);
    list.innerHTML = ytBannerHtml() + biliBannerHtml() + items.map((it, i) => cardHtml(it, names[i])).join('');
  }
  updateTip();
}

function updateTip() {
  const tip = document.querySelector('.tip');
  if (!tip) return;
  const { videos, audios } = ytStreams();
  if (videos.length && audios.length) {
    tip.textContent = '用上面的「合并下载」拿完整视频；单条流只有画面或只有声音';
  } else if (videos.length) {
    tip.textContent = '只抓到画面，声音轨还没出现 —— 播放几秒后点右上角刷新';
  } else {
    tip.textContent = '提示：打开页面后点一下播放，能抓到更多清晰度';
  }
}

function cardOf(itemId) {
  return document.querySelector(`.card[data-id="${itemId}"]`);
}

/* ---------------- 卡片状态 ---------------- */

function setBusy(itemId, text) {
  const card = cardOf(itemId);
  if (!card) return;
  card.classList.add('busy');
  card.classList.remove('done', 'failed');
  const bar = card.querySelector('.bar');
  const t = card.querySelector('.ptext');
  if (bar) bar.style.width = '3%';
  if (t) t.textContent = text || '准备中…';
}

function setProgress(itemId, percent, text) {
  const card = cardOf(itemId);
  if (!card) return;
  card.classList.add('busy');
  const bar = card.querySelector('.bar');
  const t = card.querySelector('.ptext');
  if (bar) bar.style.width = Math.max(3, Math.min(100, percent)).toFixed(1) + '%';
  if (t) t.textContent = text;
}

function finishCard(itemId, text, ok) {
  const card = cardOf(itemId);
  if (card) {
    card.classList.remove('busy');
    card.classList.add(ok ? 'done' : 'failed');
    const bar = card.querySelector('.bar');
    if (bar) bar.style.width = '100%';
    setTimeout(() => card.classList.remove('done', 'failed'), 4000);
  }
  toast(text);
}

/* ---------------- 下载流程 ---------------- */

async function handleDownload(item) {
  const filename = guessFilename(item);

  if (item.kind === 'file') {
    setBusy(item.id, '交给浏览器下载…');
    const res = await send({ type: 'download-direct', url: item.url, filename });
    if (!res.ok) {
      finishCard(item.id, '下载失败：' + (res.error || '未知原因'), false);
      return;
    }
    finishCard(item.id, '已开始下载', true);
    return;
  }

  if (item.kind === 'hls') {
    setBusy(item.id, '解析播放列表…');
    const res = await send({ type: 'hls-parse', url: item.url, pageUrl: currentTab.url });
    if (!res.ok) {
      finishCard(item.id, '解析失败：' + res.error, false);
      return;
    }
    const info = res.info || {};
    if (info.drm) {
      finishCard(item.id, 'DRM 保护（' + info.drm + '），无法下载', false);
      return;
    }
    if (info.kind === 'master' && info.variants && info.variants.length > 1) {
      const card = cardOf(item.id);
      if (card) card.classList.remove('busy');
      showVariants(item, info.variants, filename);
    } else {
      startHls(item.url, filename, item.id);
    }
    return;
  }

  if (item.kind === 'bilibili-dash') {
    setBusy(item.id, '准备下载…');
    const res = await send({
      type: 'yt-download',
      videoUrl: item.meta.videoUrl,
      audioUrl: item.meta.audioUrl,
      filename: guessFilename(item),
      pageUrl: currentTab.url
    });
    if (!res.ok) {
      finishCard(item.id, '启动失败：' + res.error, false);
      return;
    }
    tasks.set(res.taskId, item.id);
    return;
  }

  if (item.kind === 'bilibili-direct') {
    setBusy(item.id, '交给浏览器下载…');
    const res = await send({ type: 'download-direct', url: item.url, filename: guessFilename(item) });
    if (!res.ok) {
      finishCard(item.id, '下载失败：' + (res.error || '未知原因'), false);
      return;
    }
    finishCard(item.id, '已开始下载', true);
    return;
  }

  if (item.kind === 'bilibili-audio') {
    setBusy(item.id, '交给浏览器下载…');
    const res = await send({ type: 'download-direct', url: item.url, filename: guessFilename(item) });
    if (!res.ok) {
      finishCard(item.id, '下载失败：' + (res.error || '未知原因'), false);
      return;
    }
    finishCard(item.id, '已开始下载', true);
    return;
  }

  if (item.kind === 'youtube') {
    toast('YouTube 音视频分离，单条流不完整 —— 用项目里的 ytdl.py 下载');
    return;
  }

  toast('DASH 暂不支持下载。如果带 DRM，换任何工具都下不了');
}

async function startHls(url, filename, itemId) {
  setBusy(itemId, '准备下载…');
  const res = await send({ type: 'hls-download', url, pageUrl: currentTab.url, filename });
  if (!res.ok) {
    finishCard(itemId, '启动失败：' + res.error, false);
    return;
  }
  tasks.set(res.taskId, itemId);
}

async function startYtDownload() {
  const { videos, audios } = ytStreams();
  const v = videos[0];
  const a = audios[0];

  if (!v) {
    toast('没有找到画面轨');
    return;
  }
  if (!a) {
    toast('声音轨还没抓到，播放几秒后再刷新');
    return;
  }

  setBusy('yt-full', '准备下载…');
  const res = await send({
    type: 'yt-download',
    videoUrl: v.url,
    audioUrl: a.url,
    filename: guessFilename(v),
    pageUrl: currentTab.url
  });

  if (!res.ok) {
    finishCard('yt-full', '启动失败：' + res.error, false);
    return;
  }
  tasks.set(res.taskId, 'yt-full');
}

/* ---------------- 清晰度选择 ---------------- */

function showVariants(item, variants, filename) {
  const body = $('#sheet-body');
  $('#sheet-title').textContent = `选择清晰度（${variants.length}）`;

  body.innerHTML = variants
    .map((v, i) => {
      const res = v.resolution || '未知分辨率';
      const mbps = v.bandwidth ? (v.bandwidth / 1000000).toFixed(1) + ' Mbps' : '';
      const sub = [mbps, v.codecs].filter(Boolean).join(' · ') || '—';
      return `<div class="variant" data-index="${i}">
        <div>
          <div class="v-main">${esc(res)}</div>
          <div class="v-sub">${esc(sub)}</div>
        </div>
        <span class="v-go">下载</span>
      </div>`;
    })
    .join('');

  body.querySelectorAll('.variant').forEach((el) => {
    el.addEventListener('click', () => {
      const v = variants[Number(el.dataset.index)];
      hideOverlay();
      startHls(v.url, filename, item.id);
    });
  });

  $('#overlay').classList.remove('hidden');
}

function hideOverlay() {
  $('#overlay').classList.add('hidden');
}

/* ---------------- toast ---------------- */

let toastTimer = null;

function toast(text) {
  const el = $('#toast');
  el.textContent = text;
  el.classList.add('show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove('show'), 2600);
}

/* ---------------- 数据加载 ---------------- */

async function refresh() {
  if (!currentTab || currentTab.id == null) return;

  const [listRes, pageRes] = await Promise.all([
    send({ type: 'list', tabId: currentTab.id }),
    send({ type: 'page-videos', tabId: currentTab.id })
  ]);

  items = (listRes && listRes.items) || [];
  pageInfo = pageRes && pageRes.ok ? pageRes.data : null;
  render();
}

async function clearAll() {
  if (!currentTab || currentTab.id == null) return;
  await send({ type: 'clear', tabId: currentTab.id });
  items = [];
  render();
  toast('已清空本页记录');
}

async function manualDownload() {
  const input = $('#manual-url');
  const url = input.value.trim();
  if (!url) return;
  if (!/^https?:\/\//i.test(url)) {
    toast('请输入 http(s) 开头的链接');
    return;
  }

  const item = {
    id: 'manual-' + Date.now().toString(36),
    url,
    kind: /\.m3u8?(?:[?#]|$)/i.test(url) ? 'hls' : 'file',
    size: 0,
    mime: ''
  };

  items.unshift(item);
  render();
  input.value = '';
  await handleDownload(item);
}

/* ---------------- 事件 ---------------- */

chrome.runtime.onMessage.addListener((msg) => {
  if (!msg || msg.target !== 'popup') return;

  if (msg.type === 'progress') {
    const itemId = tasks.get(msg.taskId);
    if (!itemId) return;
    const pct = msg.percent || 0;
    const text =
      msg.label ||
      (msg.total ? `${msg.done}/${msg.total} · ${pct.toFixed(0)}%` : pct.toFixed(0) + '%');
    setProgress(itemId, pct, text);
    return;
  }

  if (msg.type === 'task-done') {
    const itemId = tasks.get(msg.taskId);
    tasks.delete(msg.taskId);
    if (itemId) finishCard(itemId, '合并完成，已开始保存', true);
    return;
  }

  if (msg.type === 'task-error') {
    const itemId = tasks.get(msg.taskId);
    tasks.delete(msg.taskId);
    if (itemId) finishCard(itemId, '下载失败：' + msg.error, false);
    return;
  }

  if (msg.type === 'native-download-failed') {
    toast('浏览器下载中断，可复制链接换其它方式');
  }
});

async function startBiliDownload() {
  const { dash } = biliStreams();
  const v = dash[0];
  if (!v) {
    toast('没有找到 B 站视频流');
    return;
  }
  if (!v.meta.audioUrl) {
    toast('声音轨还没读到，播放几秒后再刷新');
    return;
  }
  setBusy('bili-full', '准备下载…');
  const res = await send({
    type: 'yt-download',
    videoUrl: v.meta.videoUrl,
    audioUrl: v.meta.audioUrl,
    filename: guessFilename(v),
    pageUrl: currentTab.url
  });
  if (!res.ok) {
    finishCard('bili-full', '启动失败：' + res.error, false);
    return;
  }
  tasks.set(res.taskId, 'bili-full');
}

document.addEventListener('click', async (e) => {
  const btn = e.target.closest('button[data-act]');
  if (!btn) return;

  if (btn.dataset.act === 'yt-full') {
    await startYtDownload();
    return;
  }

  if (btn.dataset.act === 'bili-full') {
    await startBiliDownload();
    return;
  }

  const card = btn.closest('.card');
  if (!card) return;
  const item = items.find((x) => x.id === card.dataset.id);
  if (!item) return;

  if (btn.dataset.act === 'copy') {
    try {
      await navigator.clipboard.writeText(item.url);
      toast('链接已复制');
    } catch (_) {
      toast('复制失败，请手动选择');
    }
    return;
  }

  if (btn.dataset.act === 'download') {
    await handleDownload(item);
  }
});

/* ---------------- 启动 ---------------- */

async function init() {
  try {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    currentTab = tab || {};
  } catch (_) {
    currentTab = {};
  }

  try {
    $('#host').textContent = new URL(currentTab.url).hostname;
  } catch (_) {
    $('#host').textContent = '当前标签页';
  }

  $('#refresh').addEventListener('click', refresh);
  $('#clear').addEventListener('click', clearAll);
  $('#manual-go').addEventListener('click', manualDownload);
  $('#manual-url').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') manualDownload();
  });
  $('#sheet-close').addEventListener('click', hideOverlay);
  $('#overlay').addEventListener('click', (e) => {
    if (e.target.id === 'overlay') hideOverlay();
  });

  await refresh();
}

init();
