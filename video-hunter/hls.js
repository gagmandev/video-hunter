'use strict';

/* ============================================================
 * HLS 引擎
 * - 解析 master / media playlist
 * - 并发下载分片（默认 6 路），失败自动重试
 * - 支持 AES-128 解密、EXT-X-MAP(init segment)、BYTERANGE
 * - 合并成单个 Blob 并触发浏览器保存
 * ============================================================ */

const HLS = (() => {
  const CONCURRENCY = 6;
  const RETRY = 3;

  /* ---------- 工具 ---------- */

  function resolveUrl(uri, base) {
    try {
      return new URL(uri, base).href;
    } catch (_) {
      return uri;
    }
  }

  /** 解析 HLS 属性串：KEY=VALUE,KEY="V,AL,UE" */
  function parseAttrs(str) {
    const out = {};
    const re = /([A-Za-z0-9-]+)=("[^"]*"|[^,]*)/g;
    let m;
    while ((m = re.exec(str)) !== null) {
      let v = m[2] == null ? '' : m[2];
      if (v.startsWith('"') && v.endsWith('"')) v = v.slice(1, -1);
      out[m[1].toUpperCase()] = v;
    }
    return out;
  }

  function hexToBytes(hex) {
    const clean = String(hex).replace(/^0x/i, '').replace(/[^0-9a-f]/gi, '');
    const len = Math.floor(clean.length / 2);
    const out = new Uint8Array(len);
    for (let i = 0; i < len; i++) out[i] = parseInt(clean.substr(i * 2, 2), 16);
    return out;
  }

  /**
   * 判断是否 DRM 保护。
   * 注意：DRM 不限制拿链接，限制的是密钥——分片能下，但 JS 解不开。
   * 返回 DRM 系统名称，不是 DRM 则返回 null。
   */
  function detectDrm(method, uri) {
    const m = String(method || '').toUpperCase();
    const u = String(uri || '');

    if (m.startsWith('SAMPLE-AES')) {
      return /skd:/i.test(u) ? 'FairPlay' : 'Widevine / PlayReady';
    }
    if (/^skd:\/\//i.test(u)) return 'FairPlay';
    // 普通 AES-128 的密钥地址一定是 http(s) 或 data:，其余协议基本是 DRM
    if (u && !/^https?:\/\//i.test(u) && !/^data:/i.test(u)) {
      return '未知密钥协议（' + u.slice(0, 24) + '）';
    }
    return null;
  }

  function drmError(system) {
    return new Error(
      `这是 DRM 保护的视频（${system}）。分片能下载，但密钥在浏览器的 CDM 里，JS 拿不到，无法还原成可播放的文件`
    );
  }

  function seqToIv(seq) {
    const iv = new Uint8Array(16);
    new DataView(iv.buffer).setUint32(12, seq >>> 0, false);
    return iv;
  }

  /* ---------- 网络 ---------- */

  async function fetchText(url) {
    const res = await fetch(url, { credentials: 'include', cache: 'no-store' });
    if (!res.ok) throw new Error(`HTTP ${res.status} — ${url.slice(0, 100)}`);
    return await res.text();
  }

  async function fetchBuf(url, range) {
    let lastErr;
    for (let attempt = 0; attempt < RETRY; attempt++) {
      try {
        const init = { credentials: 'include' };
        if (range) {
          const end = range.offset + range.length - 1;
          init.headers = { Range: `bytes=${range.offset}-${end}` };
        }
        const res = await fetch(url, init);
        if (!res.ok && res.status !== 206) throw new Error(`HTTP ${res.status}`);
        return await res.arrayBuffer();
      } catch (e) {
        lastErr = e;
        await new Promise((r) => setTimeout(r, 250 * (attempt + 1)));
      }
    }
    throw lastErr || new Error('下载失败');
  }

  /* ---------- 播放列表解析 ---------- */

  function parseMaster(text, baseUrl) {
    const lines = text.split(/\r?\n/);
    const variants = [];
    let drm = null;

    for (let i = 0; i < lines.length; i++) {
      const line = lines[i].trim();

      if (line.toUpperCase().startsWith('#EXT-X-SESSION-KEY')) {
        const a = parseAttrs(line.slice(line.indexOf(':') + 1));
        const d = detectDrm(a.METHOD, a.URI ? resolveUrl(a.URI, baseUrl) : '');
        if (d) drm = d;
        continue;
      }

      if (!line.toUpperCase().startsWith('#EXT-X-STREAM-INF')) continue;

      const attrs = parseAttrs(line.slice(line.indexOf(':') + 1));
      let j = i + 1;
      while (j < lines.length && (!lines[j].trim() || lines[j].trim().startsWith('#'))) j++;
      if (j >= lines.length) break;

      const res = attrs.RESOLUTION || '';
      variants.push({
        url: resolveUrl(lines[j].trim(), baseUrl),
        bandwidth: parseInt(attrs.BANDWIDTH || attrs['AVERAGE-BANDWIDTH'] || '0', 10) || 0,
        resolution: res,
        codecs: attrs.CODECS || '',
        name: attrs.NAME || ''
      });
      i = j;
    }
    variants.sort((a, b) => b.bandwidth - a.bandwidth);
    return { variants, drm };
  }

  function parseMedia(text, baseUrl) {
    const lines = text.split(/\r?\n/);
    const segments = [];
    let key = null;
    let mapUri = null;
    let mapRange = null;
    let mapKey = null;
    let drm = null;
    let mediaSeq = 0;
    let duration = 0;
    let isFmp4 = false;
    let pendingRange = null;
    let lastEnd = 0;

    for (const raw of lines) {
      const line = raw.trim();
      if (!line) continue;

      if (line.startsWith('#EXT-X-MEDIA-SEQUENCE:')) {
        mediaSeq = parseInt(line.split(':')[1], 10) || 0;
      } else if (line.startsWith('#EXT-X-KEY:')) {
        const a = parseAttrs(line.slice(line.indexOf(':') + 1));
        if (a.METHOD && a.METHOD.toUpperCase() !== 'NONE') {
          key = {
            method: a.METHOD.toUpperCase(),
            uri: a.URI ? resolveUrl(a.URI, baseUrl) : '',
            iv: a.IV || ''
          };
          const d = detectDrm(key.method, key.uri);
          if (d) drm = d;
        } else {
          key = null;
        }
      } else if (line.startsWith('#EXT-X-MAP:')) {
        const a = parseAttrs(line.slice(line.indexOf(':') + 1));
        if (a.URI) {
          mapUri = resolveUrl(a.URI, baseUrl);
          isFmp4 = true;
          mapKey = key ? { ...key } : null;
          if (a.BYTERANGE) {
            const [len, off] = a.BYTERANGE.split('@');
            mapRange = { length: parseInt(len, 10), offset: off ? parseInt(off, 10) : 0 };
          }
        }
      } else if (line.startsWith('#EXT-X-BYTERANGE:')) {
        const [len, off] = line.slice(line.indexOf(':') + 1).split('@');
        const length = parseInt(len, 10);
        pendingRange = {
          length,
          offset: off != null ? parseInt(off, 10) : lastEnd
        };
      } else if (line.startsWith('#EXTINF:')) {
        duration += parseFloat(line.slice(line.indexOf(':') + 1)) || 0;
      } else if (!line.startsWith('#')) {
        const url = resolveUrl(line, baseUrl);
        segments.push({ url, key: key ? { ...key } : null, range: pendingRange });
        if (pendingRange) lastEnd = pendingRange.offset + pendingRange.length;
        pendingRange = null;
      }
    }

    return {
      type: 'media',
      segments,
      mapUri,
      mapRange,
      mapKey,
      drm,
      mediaSeq,
      duration,
      isFmp4,
      isLive: !text.includes('#EXT-X-ENDLIST')
    };
  }

  async function loadPlaylist(url) {
    const text = await fetchText(url);
    if (!text || text.trim().length === 0) throw new Error('播放列表内容为空');
    if (text.includes('#EXT-X-STREAM-INF')) {
      const parsed = parseMaster(text, url);
      return { type: 'master', variants: parsed.variants, sessionDrm: parsed.drm, url };
    }
    return { ...parseMedia(text, url), url };
  }

  /* ---------- 解密 ---------- */

  const keyCache = new Map();

  async function getKey(uri) {
    if (!uri) throw new Error('缺少密钥地址');
    if (keyCache.has(uri)) return keyCache.get(uri);
    const p = fetchBuf(uri).then((buf) =>
      crypto.subtle.importKey('raw', buf, { name: 'AES-CBC' }, false, ['decrypt'])
    );
    keyCache.set(uri, p);
    return p;
  }

  async function decryptSegment(buf, keyInfo, seq) {
    try {
      const key = await getKey(keyInfo.uri);
      let iv;
      if (keyInfo.iv) {
        const bytes = hexToBytes(keyInfo.iv);
        iv = bytes.length === 16 ? bytes : seqToIv(seq);
      } else {
        iv = seqToIv(seq);
      }
      return await crypto.subtle.decrypt({ name: 'AES-CBC', iv }, key, buf);
    } catch (e) {
      // 解密失败绝不能返回密文，否则用户拿到的是一个坏文件
      throw new Error(
        `第 ${seq} 个分片解密失败（${String((e && e.message) || e)}），` +
          '该视频可能使用了不支持的加密方式'
      );
    }
  }

  /* ---------- 并发池 ---------- */

  async function pool(items, limit, worker) {
    let cursor = 0;
    const runners = [];
    const n = Math.min(limit, items.length);
    for (let i = 0; i < n; i++) {
      runners.push(
        (async () => {
          while (true) {
            const idx = cursor++;
            if (idx >= items.length) break;
            await worker(items[idx], idx);
          }
        })()
      );
    }
    await Promise.all(runners);
  }

  /* ---------- 保存 ---------- */

  function saveBlob(blob, filename) {
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = filename;
    a.style.display = 'none';
    document.body.appendChild(a);
    a.click();
    setTimeout(() => {
      try {
        a.remove();
      } catch (_) { /* ignore */ }
      URL.revokeObjectURL(url);
    }, 120000);
  }

  function fixExtension(filename, isFmp4) {
    const base = String(filename || 'video').replace(/\.(ts|mp4|m4v|webm|mkv)$/i, '');
    return base + (isFmp4 ? '.mp4' : '.ts');
  }

  /* ---------- 对外：解析 ---------- */

  async function parse(url) {
    const info = await loadPlaylist(url);
    if (info.type === 'master') {
      if (!info.variants.length) throw new Error('主播放列表中没有可用清晰度');
      // 顺带解析第一个（最高码率）分片数，方便界面显示
      let preview = null;
      try {
        preview = await loadPlaylist(info.variants[0].url);
      } catch (_) { /* 忽略 */ }

      const drm = info.sessionDrm || (preview && preview.drm) || null;
      return {
        kind: 'master',
        variants: info.variants,
        segments: preview && preview.segments ? preview.segments.length : 0,
        duration: preview ? preview.duration : 0,
        drm
      };
    }

    return {
      kind: 'media',
      variants: [],
      segments: info.segments.length,
      duration: info.duration,
      isFmp4: info.isFmp4,
      isLive: info.isLive,
      drm: info.drm || null
    };
  }

  /* ---------- 对外：下载 ---------- */

  async function download(url, filename, onProgress) {
    keyCache.clear();

    let info = await loadPlaylist(url);
    if (info.type === 'master') {
      if (info.sessionDrm) throw drmError(info.sessionDrm);
      if (!info.variants.length) throw new Error('主播放列表中没有可用清晰度');
      info = await loadPlaylist(info.variants[0].url);
      if (info.type === 'master') throw new Error('播放列表嵌套层级过深');
    }

    if (info.drm) throw drmError(info.drm);

    const segments = info.segments || [];
    if (!segments.length) {
      throw new Error('播放列表里没有分片，可能是直播流或该格式不受支持');
    }

    const unsupported = segments.find(
      (s) => s.key && s.key.method && s.key.method !== 'AES-128'
    );
    if (unsupported) {
      throw new Error(
        `加密方式 ${unsupported.key.method} 不支持（属于 DRM 保护，无法下载）`
      );
    }

    const offset = info.mapUri ? 1 : 0;
    const parts = new Array(segments.length + offset);

    if (info.mapUri) {
      let init = await fetchBuf(info.mapUri, info.mapRange);
      if (info.mapKey && info.mapKey.method === 'AES-128') {
        init = await decryptSegment(init, info.mapKey, info.mediaSeq);
      }
      parts[0] = init;
    }

    const total = segments.length;
    let done = 0;
    let bytes = 0;
    let lastReport = 0;

    const report = (force) => {
      const now = Date.now();
      if (!force && now - lastReport < 120) return;
      lastReport = now;
      if (onProgress) {
        onProgress({
          done,
          total,
          bytes,
          percent: total ? (done / total) * 100 : 0,
          duration: info.duration
        });
      }
    };

    await pool(segments, CONCURRENCY, async (seg, idx) => {
      let buf = await fetchBuf(seg.url, seg.range);
      if (seg.key && seg.key.method === 'AES-128') {
        buf = await decryptSegment(buf, seg.key, info.mediaSeq + idx);
      }
      parts[idx + offset] = buf;
      done++;
      bytes += buf.byteLength;
      report(false);
    });

    report(true);

    const blob = new Blob(parts.filter(Boolean), {
      type: info.isFmp4 ? 'video/mp4' : 'video/mp2t'
    });

    if (blob.size === 0) throw new Error('合并后的文件为空');

    const name = fixExtension(filename, info.isFmp4);
    saveBlob(blob, name);

    return { size: blob.size, name, segments: total };
  }

  return { parse, download, loadPlaylist };
})();
