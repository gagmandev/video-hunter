'use strict';

/* ============================================================
 * offscreen 文档：承担需要 DOM 的重活
 * - fetch 分片
 * - Blob 组装
 * - 触发浏览器下载
 * ============================================================ */

function reply(sendResponse, fn) {
  Promise.resolve()
    .then(fn)
    .then((res) => sendResponse(res))
    .catch((e) => sendResponse({ ok: false, error: String((e && e.message) || e) }));
}

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
  }, 180000);
}

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (!msg || msg.target !== 'offscreen') return; // 不占用响应通道

  if (msg.type === 'hls-parse') {
    reply(sendResponse, async () => {
      const info = await HLS.parse(msg.url);
      return { ok: true, info };
    });
    return true;
  }

  if (msg.type === 'hls-download') {
    reply(sendResponse, async () => {
      let lastSent = 0;
      const result = await HLS.download(msg.url, msg.filename, (p) => {
        const now = Date.now();
        if (now - lastSent < 150 && p.percent < 100) return;
        lastSent = now;
        chrome.runtime
          .sendMessage({
            target: 'popup',
            type: 'progress',
            taskId: msg.taskId,
            percent: p.percent,
            done: p.done,
            total: p.total,
            bytes: p.bytes
          })
          .catch(() => {});
      });
      return { ok: true, result };
    });
    return true;
  }

  if (msg.type === 'yt-download') {
    reply(sendResponse, async () => {
      const merger = YTDownload.createMerger(window.MP4Box, window.Mp4Muxer);

      let lastSent = 0;
      const report = (percent, label) => {
        const now = Date.now();
        if (now - lastSent < 200 && percent < 100) return;
        lastSent = now;
        chrome.runtime
          .sendMessage({
            target: 'popup',
            type: 'progress',
            taskId: msg.taskId,
            percent,
            label
          })
          .catch(() => {});
      };

      report(1, '下载画面…');
      const videoBlob = await YTDownload.fetchFull(msg.videoUrl, (p) => {
        report(p * 45, `下载画面 ${Math.round(p * 100)}%`);
      });

      let audioBuf = null;
      if (msg.audioUrl) {
        report(46, '下载声音…');
        const audioBlob = await YTDownload.fetchFull(msg.audioUrl, (p) => {
          report(46 + p * 14, `下载声音 ${Math.round(p * 100)}%`);
        });
        audioBuf = await audioBlob.arrayBuffer();
      }

      report(60, '合并音视频…');
      const videoBuf = await videoBlob.arrayBuffer();
      const merged = await merger.merge(videoBuf, audioBuf, (p, label) => {
        report(60 + p * 35, label || '合并中…');
      });

      report(97, '生成文件…');
      const blob = new Blob([merged], { type: 'video/mp4' });
      saveBlob(blob, msg.filename);

      return { ok: true, result: { size: blob.size, name: msg.filename } };
    });
    return true;
  }

  if (msg.type === 'ping') {
    sendResponse({ ok: true });
    return;
  }
});
