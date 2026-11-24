'use strict';

/* ============================================================
 * 视频猎手 · background service worker
 * 职责：监听网络请求嗅探媒体资源 / 维护每个标签页的列表 /
 *       转发下载任务给 offscreen 文档 / 处理 Referer 伪装规则
 * ============================================================ */

const STORAGE_PREFIX = 'tab_';
const MAX_ITEMS = 80;
const MIN_SIZE = 30 * 1024; // 小于 30KB 的当作广告 / 预览图忽略

/* ---------------- 媒体类型识别 ---------------- */

const HLS_RE = /\.m3u8?(?:[?#]|$)/i;
const DASH_RE = /\.mpd(?:[?#]|$)/i;
const SEGMENT_RE = /\.(ts|m4s|cmfv|cmfa|aac)(?:[?#]|$)/i;
const FILE_RE = /\.(mp4|m4v|webm|mkv|mov|flv|f4v|ogv|avi|wmv|3gp)(?:[?#]|$)/i;
// YouTube 的 DASH 流没有扩展名，路径固定是 /videoplayback
const YT_RE = /googlevideo\.com\/videoplayback/i;
// 部分 CDN 把 MIME 藏在查询参数里，响应头只给 application/octet-stream
const MIME_PARAM_RE = /[?&]mime=(?:video|audio)(?:%2F|\/)/i;

function classify(url, mime) {
  const u = String(url || '').split('#')[0];
  const m = String(mime || '').toLowerCase();

  // 必须优先判断：YouTube 的 URL 没有扩展名，靠 MIME 也未必认得出来
  if (YT_RE.test(u)) return 'youtube';

  if (HLS_RE.test(u) || m.includes('mpegurl')) return 'hls';
  if (DASH_RE.test(u) || m.includes('dash+xml')) return 'dash';
  if (SEGMENT_RE.test(u) || m.includes('mp2t')) return 'segment';
  if (FILE_RE.test(u) || m.startsWith('video/')) return 'file';
  if (MIME_PARAM_RE.test(u)) return 'file';

  return null;
}

/** YouTube 同一条流会按 range 拆成很多请求，按 itag 归并成一条 */
function itemKey(item) {
  if (item.kind === 'youtube') {
    const m = String(item.url).match(/[?&]itag=(\d+)/);
    if (m) return 'yt:' + m[1];
  }
  return item.url;
}

function shortHash(str) {
  let h = 5381;
  for (let i = 0; i < str.length; i++) h = ((h << 5) + h + str.charCodeAt(i)) >>> 0;
  return h.toString(36);
}

/* ---------------- 每个标签页的缓存 ---------------- */

/** tabId -> Map(url -> item) */
const cache = new Map();
const dirty = new Set();
let flushTimer = null;

function addItem(tabId, item) {
  let map = cache.get(tabId);
  if (!map) {
    map = new Map();
    cache.set(tabId, map);
  }
  const key = itemKey(item);
  const prev = map.get(key);
  if (prev) {
    if (item.size > (prev.size || 0)) prev.size = item.size;
    if (item.mime) prev.mime = item.mime;
    prev.at = item.at;
    prev.url = item.url;
  } else {
    map.set(key, { ...item, id: shortHash(key) });
    while (map.size > MAX_ITEMS) {
      map.delete(map.keys().next().value);
    }
  }
  dirty.add(tabId);
  scheduleFlush();
  updateBadge(tabId);
}

function scheduleFlush() {
  if (flushTimer) return;
  flushTimer = setTimeout(flush, 400);
}

async function flush() {
  flushTimer = null;
  const tabs = [...dirty];
  dirty.clear();
  if (!tabs.length) return;
  const payload = {};
  for (const tabId of tabs) {
    const map = cache.get(tabId);
    payload[STORAGE_PREFIX + tabId] = map ? [...map.values()] : [];
  }
  try {
    await chrome.storage.session.set(payload);
  } catch (_) {
    /* session 配额问题，忽略 */
  }
}

async function getItems(tabId) {
  let map = cache.get(tabId);
  if (!map) {
    const key = STORAGE_PREFIX + tabId;
    let arr = [];
    try {
      const got = await chrome.storage.session.get(key);
      arr = got[key] || [];
    } catch (_) { /* ignore */ }
    map = new Map(arr.map((x) => [x.url, x]));
    cache.set(tabId, map);
  }
  return [...map.values()].sort((a, b) => (b.at || 0) - (a.at || 0));
}

function clearTab(tabId) {
  cache.delete(tabId);
  dirty.delete(tabId);
  chrome.storage.session.remove(STORAGE_PREFIX + tabId).catch(() => {});
  chrome.action.setBadgeText({ tabId, text: '' }).catch(() => {});
}

function updateBadge(tabId) {
  const map = cache.get(tabId);
  const n = map ? map.size : 0;
  chrome.action.setBadgeText({ tabId, text: n ? String(n) : '' }).catch(() => {});
  if (n) {
    chrome.action.setBadgeBackgroundColor({ tabId, color: '#6C5CE7' }).catch(() => {});
  }
}

/* ---------------- 网络嗅探 ---------------- */

function onResponseStarted(details) {
  const tabId = details.tabId;
  if (tabId == null || tabId < 0) return;
  const url = details.url;
  if (!/^https?:/i.test(url)) return;

  let mime = '';
  let size = 0;
  const headers = details.responseHeaders;
  if (headers) {
    for (const h of headers) {
      const name = (h.name || '').toLowerCase();
      if (name === 'content-type') mime = h.value || '';
      else if (name === 'content-length') size = parseInt(h.value, 10) || 0;
    }
  }

  const kind = classify(url, mime);
  if (!kind) return;
  if (kind === 'segment') return; // HLS / DASH 分片不单独展示

  // YouTube 的 URL 带 clen 参数，是整条流的真实长度
  if (kind === 'youtube' && !size) {
    const m = url.match(/[?&]clen=(\d+)/);
    if (m) size = parseInt(m[1], 10);
  }

  const isPlaylist = kind === 'hls' || kind === 'dash';
  if (!isPlaylist && size > 0 && size < MIN_SIZE) return;

  addItem(tabId, {
    url,
    kind,
    mime: (mime.split(';')[0] || '').trim(),
    size,
    at: Date.now()
  });
}

try {
  chrome.webRequest.onResponseStarted.addListener(
    onResponseStarted,
    { urls: ['<all_urls>'], types: ['media', 'xmlhttprequest', 'other', 'object'] },
    ['responseHeaders']
  );
} catch (e) {
  console.error('[VideoHunter] webRequest 注册失败', e);
}

chrome.tabs.onRemoved.addListener(clearTab);
chrome.tabs.onUpdated.addListener((tabId, info) => {
  // 页面开始导航就重置，否则刷新同一地址时旧列表会残留
  if (info.status === 'loading') clearTab(tabId);
});
chrome.tabs.onActivated.addListener(async ({ tabId }) => {
  try {
    const items = await getItems(tabId);
    chrome.action.setBadgeText({ tabId, text: items.length ? String(items.length) : '' }).catch(() => {});
  } catch (_) { /* ignore */ }
});

/* ---------------- offscreen 文档管理 ---------------- */

let creatingOffscreen = null;

async function ensureOffscreen() {
  try {
    const contexts = await chrome.runtime.getContexts({ contextTypes: ['OFFSCREEN_DOCUMENT'] });
    if (contexts && contexts.length > 0) return;
  } catch (_) { /* 老版本没有 getContexts */ }

  if (creatingOffscreen) return creatingOffscreen;

  creatingOffscreen = chrome.offscreen
    .createDocument({
      url: 'offscreen.html',
      reasons: ['BLOBS'],
      justification: '下载 HLS 分片并在本地合并成完整视频文件'
    })
    .catch((err) => {
      const msg = String((err && err.message) || err);
      if (!/single offscreen|already exists/i.test(msg)) throw err;
    })
    .finally(() => {
      creatingOffscreen = null;
    });

  return creatingOffscreen;
}

function sendToOffscreen(payload, timeout = 60000) {
  return new Promise((resolve, reject) => {
    let settled = false;
    const timer = setTimeout(() => {
      if (settled) return;
      settled = true;
      reject(new Error('后台任务超时'));
    }, timeout);

    try {
      chrome.runtime.sendMessage({ ...payload, target: 'offscreen' }, (res) => {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        if (chrome.runtime.lastError) {
          reject(new Error(chrome.runtime.lastError.message));
          return;
        }
        if (!res) {
          reject(new Error('后台无响应'));
          return;
        }
        if (res.ok === false) {
          reject(new Error(res.error || '未知错误'));
          return;
        }
        resolve(res);
      });
    } catch (e) {
      settled = true;
      clearTimeout(timer);
      reject(e);
    }
  });
}

/* ---------------- Referer / Origin 伪装 ---------------- */

const ruleIds = new Map(); // taskId -> ruleId
let nextRuleId = 9301;

async function setRefererRule(taskId, pageUrl) {
  if (!pageUrl || !/^https?:/i.test(pageUrl)) return null;
  let origin;
  try {
    origin = new URL(pageUrl).origin;
  } catch (_) {
    return null;
  }

  const id = nextRuleId++;
  try {
    await chrome.declarativeNetRequest.updateSessionRules({
      addRules: [
        {
          id,
          priority: 1,
          action: {
            type: 'modifyHeaders',
            requestHeaders: [
              { header: 'Referer', operation: 'set', value: pageUrl },
              { header: 'Origin', operation: 'set', value: origin }
            ]
          },
          condition: {
            initiatorDomains: [chrome.runtime.id],
            resourceTypes: ['xmlhttprequest', 'media', 'other', 'object']
          }
        }
      ]
    });
    ruleIds.set(taskId, id);
    return id;
  } catch (e) {
    console.warn('[VideoHunter] 设置 Referer 规则失败（不影响主流程）', e);
    return null;
  }
}

async function clearRefererRule(taskId) {
  const id = ruleIds.get(taskId);
  if (!id) return;
  ruleIds.delete(taskId);
  try {
    await chrome.declarativeNetRequest.updateSessionRules({ removeRuleIds: [id] });
  } catch (_) { /* ignore */ }
}

/* ---------------- 业务处理 ---------------- */

function sanitizeFilename(name) {
  return String(name || 'video')
    .replace(/[\\/:*?"<>|\r\n\t]/g, '_')
    .replace(/\s+/g, ' ')
    .replace(/^[.\s]+/, '')
    .trim()
    .slice(0, 150) || 'video';
}

async function downloadDirect({ url, filename }) {
  const downloadId = await chrome.downloads.download({
    url,
    filename: sanitizeFilename(filename),
    saveAs: false,
    conflictAction: 'uniquify'
  });
  return { ok: true, downloadId };
}

async function hlsParse({ url, pageUrl }) {
  await ensureOffscreen();
  const probeId = 'parse_' + shortHash(url + Date.now());
  await setRefererRule(probeId, pageUrl);
  try {
    const res = await sendToOffscreen({ type: 'hls-parse', url }, 45000);
    return res;
  } finally {
    clearRefererRule(probeId);
  }
}

async function hlsDownload({ url, pageUrl, filename }) {
  await ensureOffscreen();
  const taskId = 't' + Date.now().toString(36) + Math.random().toString(36).slice(2, 6);
  await setRefererRule(taskId, pageUrl);

  sendToOffscreen(
    { type: 'hls-download', taskId, url, filename: sanitizeFilename(filename) },
    6 * 60 * 60 * 1000
  )
    .then((res) => {
      broadcast({ target: 'popup', type: 'task-done', taskId, result: res && res.result });
    })
    .catch((err) => {
      broadcast({
        target: 'popup',
        type: 'task-error',
        taskId,
        error: String((err && err.message) || err)
      });
    })
    .finally(() => {
      clearRefererRule(taskId);
    });

  return { ok: true, taskId };
}

async function ytDownload({ videoUrl, audioUrl, filename, pageUrl }) {
  await ensureOffscreen();
  const taskId = 'yt' + Date.now().toString(36) + Math.random().toString(36).slice(2, 5);
  await setRefererRule(taskId, pageUrl);

  sendToOffscreen(
    {
      type: 'yt-download',
      taskId,
      videoUrl,
      audioUrl: audioUrl || null,
      filename: sanitizeFilename(filename)
    },
    3 * 60 * 60 * 1000
  )
    .then((res) => {
      broadcast({ target: 'popup', type: 'task-done', taskId, result: res && res.result });
    })
    .catch((err) => {
      broadcast({
        target: 'popup',
        type: 'task-error',
        taskId,
        error: String((err && err.message) || err)
      });
    })
    .finally(() => {
      clearRefererRule(taskId);
    });

  return { ok: true, taskId };
}

function handlePageMedia(msg) {
  if (!msg.results || !msg.results.length) return { ok: true, count: 0 };
  const tabId = msg.tabId;
  if (tabId == null || tabId < 0) return { ok: false, error: 'invalid tab' };

  let items = [];
  if (msg.site === 'bilibili') {
    items = normalizeBilibili(msg.results, msg.title);
  }

  for (const item of items) {
    addItem(tabId, item);
  }
  return { ok: true, count: items.length };
}

async function pageVideos(tabId) {
  try {
    const results = await chrome.scripting.executeScript({
      target: { tabId },
      func: () => {
        const videos = [];
        document.querySelectorAll('video').forEach((v) => {
          videos.push({
            src: v.currentSrc || v.src || '',
            poster: v.poster || '',
            duration: Number.isFinite(v.duration) ? v.duration : 0,
            width: v.videoWidth || 0,
            height: v.videoHeight || 0
          });
        });
        return { title: document.title, url: location.href, videos };
      }
    });
    return { ok: true, data: results && results[0] && results[0].result };
  } catch (e) {
    return { ok: false, error: String((e && e.message) || e) };
  }
}

function broadcast(msg) {
  chrome.runtime.sendMessage(msg).catch(() => {});
}

function normalizeBilibili(items, title) {
  const baseTitle = String(title || 'bilibili')
    .replace(/\s*-\s*bilibili\s*\.?\s*$/i, '')
    .replace(/[\\/:*?"<>|\r\n\t]/g, '_')
    .trim();

  // 按清晰度把视频分组
  const videos = items.filter((x) => x.type === 'dash-video').sort((a, b) => (b.height || 0) - (a.height || 0));
  const audios = items.filter((x) => x.type === 'dash-audio').sort((a, b) => (b.bandwidth || 0) - (a.bandwidth || 0));
  const directs = items.filter((x) => x.type === 'direct');

  const out = [];

  // 直链最省事，直接展示
  for (const d of directs) {
    out.push({
      url: d.url,
      kind: 'bilibili-direct',
      mime: 'video/flv',
      size: d.size,
      at: Date.now(),
      meta: { title: baseTitle, duration: d.duration }
    });
  }

  // 每个清晰度生成一个可合并下载的条目
  for (let i = 0; i < videos.length; i++) {
    const v = videos[i];
    const audio = audios[0] || null;
    out.push({
      url: v.url,
      kind: 'bilibili-dash',
      mime: 'video/mp4',
      size: (v.size || 0) + (audio && audio.size ? audio.size : 0),
      at: Date.now(),
      meta: {
        title: baseTitle,
        resolution: `${v.width}x${v.height}`,
        height: v.height,
        videoUrl: v.url,
        audioUrl: audio ? audio.url : null,
        duration: v.duration,
        codec: v.codec
      }
    });
  }

  // 没有视频但有音频的极端情况
  if (!videos.length && audios.length) {
    for (const a of audios) {
      out.push({
        url: a.url,
        kind: 'bilibili-audio',
        mime: 'audio/mp4',
        size: a.size,
        at: Date.now(),
        meta: { title: baseTitle, label: a.label || '音频', duration: a.duration }
      });
    }
  }

  return out;
}

async function handle(msg) {
  switch (msg.type) {
    case 'list':
      return { ok: true, items: await getItems(msg.tabId) };
    case 'clear':
      clearTab(msg.tabId);
      return { ok: true };
    case 'download-direct':
      return await downloadDirect(msg);
    case 'hls-parse':
      return await hlsParse(msg);
    case 'hls-download':
      return await hlsDownload(msg);
    case 'yt-download':
      return await ytDownload(msg);
    case 'page-videos':
      return await pageVideos(msg.tabId);
    case 'page-media':
      return handlePageMedia(msg);
    default:
      return { ok: false, error: 'unknown message type: ' + msg.type };
  }
}

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (!msg || msg.target !== 'background') return; // 不占用响应通道
  const payload = { ...msg };
  if (payload.type === 'page-media' && sender && sender.tab && sender.tab.id != null) {
    payload.tabId = sender.tab.id;
  }
  handle(payload)
    .then(sendResponse)
    .catch((e) => sendResponse({ ok: false, error: String((e && e.message) || e) }));
  return true;
});

/* 下载进度 -> 角标 */
chrome.runtime.onMessage.addListener((msg) => {
  if (!msg || msg.target !== 'popup') return;
  if (msg.type === 'progress') {
    const pct = Math.max(0, Math.min(100, Math.round(msg.percent || 0)));
    chrome.action.setBadgeText({ text: pct + '%' }).catch(() => {});
    chrome.action.setBadgeBackgroundColor({ color: '#00B894' }).catch(() => {});
  } else if (msg.type === 'task-done' || msg.type === 'task-error') {
    setTimeout(() => {
      chrome.action.setBadgeText({ text: msg.type === 'task-done' ? '✓' : '!' }).catch(() => {});
      chrome.action.setBadgeBackgroundColor({
        color: msg.type === 'task-done' ? '#00B894' : '#D63031'
      }).catch(() => {});
    }, 0);

    // 5 秒后恢复成当前标签页的嗅探数量
    setTimeout(async () => {
      try {
        const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
        if (tab) updateBadge(tab.id);
      } catch (_) { /* ignore */ }
    }, 5000);
  }
});

/* 原生下载中断时通知界面，方便降级到 Blob 方式 */
chrome.downloads.onChanged.addListener((delta) => {
  if (delta.state && delta.state.current === 'interrupted') {
    broadcast({
      target: 'popup',
      type: 'native-download-failed',
      downloadId: delta.id,
      error: (delta.error && delta.error.current) || 'INTERRUPTED'
    });
  }
});
