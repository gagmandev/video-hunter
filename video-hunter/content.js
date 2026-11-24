'use strict';

/* ============================================================
 * 视频猎手 · content script
 * 职责：在特定站点读取页面内嵌的播放信息，补充网络嗅探的盲区
 * ============================================================ */

const SITES = {
  bilibili: /bilibili\.com\/video\/(BV|av)/i
};

function currentSite() {
  for (const [name, re] of Object.entries(SITES)) {
    if (re.test(location.href)) return name;
  }
  return null;
}

function send(payload) {
  chrome.runtime.sendMessage({ target: 'background', ...payload }).catch(() => {});
}

/* ---------------- B 站 ---------------- */

function probeBilibili() {
  const info = window.__playinfo__;
  if (!info || !info.data) return;

  const data = info.data;
  const results = [];

  // FLV / 直链格式（旧版或部分视频）
  if (Array.isArray(data.durl)) {
    for (const seg of data.durl) {
      if (seg.url) {
        results.push({
          type: 'direct',
          url: seg.url,
          size: seg.size || 0,
          order: seg.order || 0,
          duration: data.timelength || data.duration || 0
        });
      }
    }
  }

  // DASH 格式（新版主流）
  if (data.dash) {
    const dash = data.dash;
    const duration = data.timelength || data.duration || 0;

    // video 可能有多个码率
    if (Array.isArray(dash.video)) {
      for (const v of dash.video) {
        if (v.baseUrl || v.base_url) {
          results.push({
            type: 'dash-video',
            url: v.baseUrl || v.base_url,
            bandwidth: v.bandwidth || v.id || 0,
            width: v.width || 0,
            height: v.height || 0,
            codec: v.codecs || '',
            size: v.size || 0,
            duration
          });
        }
      }
    }

    if (Array.isArray(dash.audio)) {
      for (const a of dash.audio) {
        if (a.baseUrl || a.base_url) {
          results.push({
            type: 'dash-audio',
            url: a.baseUrl || a.base_url,
            bandwidth: a.bandwidth || a.id || 0,
            codec: a.codecs || '',
            size: a.size || 0,
            duration
          });
        }
      }
    }

    // 杜比 / 全景声（可选音轨）
    if (Array.isArray(dash.dolby)) {
      for (const a of dash.dolby) {
        if (a.baseUrl || a.base_url) {
          results.push({
            type: 'dash-audio',
            url: a.baseUrl || a.base_url,
            bandwidth: a.bandwidth || a.id || 0,
            codec: a.codecs || '',
            size: a.size || 0,
            duration,
            label: '杜比音效'
          });
        }
      }
    }
  }

  if (results.length) {
    send({ type: 'page-media', site: 'bilibili', results, title: document.title, url: location.href });
  }
}

/* ---------------- 探测入口 ---------------- */

function probe() {
  const site = currentSite();
  if (!site) return;
  if (site === 'bilibili') probeBilibili();
}

// 页面刚加载时 __playinfo__ 可能还没挂上去，多等一会儿
if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', () => setTimeout(probe, 800));
} else {
  setTimeout(probe, 800);
}

// SPA 跳转后 URL 变了，需要重新探测
let lastUrl = location.href;
setInterval(() => {
  if (location.href !== lastUrl) {
    lastUrl = location.href;
    setTimeout(probe, 1200);
  }
}, 1000);
