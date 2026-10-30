'use strict';

/* ============================================================
 * YouTube DASH 下载与音视频合并
 *
 * YouTube 把画面和声音拆成两条独立的流（itag 137 / 140 之类），
 * 单独下载任何一条都是残缺的。这里用 mp4box 解封装出原始帧，
 * 再用 mp4-muxer 重新封装成一个完整的 mp4——不需要 ffmpeg。
 * ============================================================ */

(function (global) {
  const CHUNK = 4 * 1024 * 1024;

  function toAB(buf) {
    let ab;
    if (buf instanceof ArrayBuffer) {
      ab = buf;
    } else if (ArrayBuffer.isView(buf)) {
      ab = buf.buffer.slice(buf.byteOffset, buf.byteOffset + buf.byteLength);
    } else {
      ab = buf;
    }
    // mp4box 靠 fileStart 判断缓冲区在文件中的位置
    ab.fileStart = 0;
    return ab;
  }

  function createMerger(MP4Box, Mp4Muxer) {
    function parseFile(buf) {
      const f = MP4Box.createFile();
      let info = null;
      f.onReady = (i) => {
        info = i;
      };
      f.onError = (e) => console.warn('[merge] 解析告警', e);
      f.appendBuffer(toAB(buf));
      f.flush();
      if (!info) throw new Error('无法解析 MP4 结构，可能不是有效的 fMP4 流');
      return { file: f, info };
    }

    function extractSamples(file, trackId, expected) {
      return new Promise((resolve, reject) => {
        const all = [];
        file.onSamples = (id, user, samples) => {
          for (let i = 0; i < samples.length; i++) all.push(samples[i]);
        };
        file.setExtractionOptions(trackId, null, { nbSamples: 1000000 });
        file.start();

        const t0 = Date.now();
        (function check() {
          if (all.length >= expected) return resolve(all);
          if (Date.now() - t0 > 30000) {
            return all.length ? resolve(all) : reject(new Error('帧提取超时'));
          }
          setTimeout(check, 30);
        })();
      });
    }

    function getAvcC(file, trackId) {
      const trak = file.getTrackById(trackId);
      const entry = trak.mdia.minf.stbl.stsd.entries[0];
      if (!entry.avcC) throw new Error('视频编码不是 H.264（缺少 avcC），暂不支持');
      const ds = new MP4Box.DataStream(undefined, 0, MP4Box.DataStream.BIG_ENDIAN);
      entry.avcC.write(ds);
      // 前 8 字节是 box 头（size + 'avcC'），description 只要内容
      return new Uint8Array(ds.buffer, 8);
    }

    function getAudioConfig(file, trackId) {
      const trak = file.getTrackById(trackId);
      const entry = trak.mdia.minf.stbl.stsd.entries[0];
      if (!entry.esds) return null;
      const dcd = (entry.esds.esd.descs || []).find((d) => d.tag === 4);
      const dsi = dcd && (dcd.descs || []).find((d) => d.tag === 5);
      return dsi ? dsi.data : null;
    }

    /**
     * @param {ArrayBuffer} videoBuf 视频轨完整 fMP4
     * @param {ArrayBuffer|null} audioBuf 音频轨完整 fMP4
     * @param {(p:number, label:string)=>void} onProgress
     * @returns {Promise<Uint8Array>}
     */
    async function merge(videoBuf, audioBuf, onProgress) {
      const report = (p, label) => {
        if (onProgress) onProgress(p, label);
      };

      report(0.03, '解析视频轨');
      const v = parseFile(videoBuf);
      const vTrack = v.info.videoTracks && v.info.videoTracks[0];
      if (!vTrack) throw new Error('视频流里没有视频轨');

      let a = null;
      let aTrack = null;
      if (audioBuf) {
        report(0.08, '解析音频轨');
        a = parseFile(audioBuf);
        aTrack = a.info.audioTracks && a.info.audioTracks[0];
      }

      report(0.12, '提取视频帧');
      const vSamples = await extractSamples(v.file, vTrack.id, vTrack.nb_samples);

      let aSamples = [];
      if (a && aTrack) {
        report(0.25, '提取音频帧');
        aSamples = await extractSamples(a.file, aTrack.id, aTrack.nb_samples);
      }

      const opts = {
        target: new Mp4Muxer.ArrayBufferTarget(),
        video: {
          codec: 'avc',
          width: vTrack.video.width,
          height: vTrack.video.height
        },
        fastStart: 'in-memory'
      };
      if (aTrack) {
        opts.audio = {
          codec: 'aac',
          numberOfChannels: aTrack.audio.channel_count,
          sampleRate: aTrack.audio.sample_rate
        };
      }

      const muxer = new Mp4Muxer.Muxer(opts);
      const vDesc = getAvcC(v.file, vTrack.id);
      const aDesc = aTrack ? getAudioConfig(a.file, aTrack.id) : null;

      report(0.4, '封装视频轨');
      for (const s of vSamples) {
        muxer.addVideoChunkRaw(
          new Uint8Array(s.data),
          s.is_sync ? 'key' : 'delta',
          (s.cts * 1e6) / s.timescale,
          (s.duration * 1e6) / s.timescale,
          { decoderConfig: { codec: vTrack.codec, description: vDesc } }
        );
      }

      if (aSamples.length) {
        report(0.7, '封装音频轨');
        for (const s of aSamples) {
          muxer.addAudioChunkRaw(
            new Uint8Array(s.data),
            'key',
            (s.cts * 1e6) / s.timescale,
            (s.duration * 1e6) / s.timescale,
            {
              decoderConfig: {
                codec: aTrack.codec,
                description: aDesc || undefined
              }
            }
          );
        }
      }

      report(0.92, '生成文件');
      muxer.finalize();
      return new Uint8Array(muxer.target.buffer);
    }

    return { merge };
  }

  /* ---------------- 分段下载 ---------------- */

  function withRange(url, start, end) {
    const r = start + '-' + end;
    if (/[?&]range=/.test(url)) return url.replace(/([?&]range=)[^&]*/, '$1' + r);
    return url + (url.indexOf('?') >= 0 ? '&' : '?') + 'range=' + r;
  }

  /**
   * 按 range 分段下载整条流。
   * 不用一次请求整个文件——YouTube 对无 range 请求会限速甚至拒绝。
   */
  async function fetchFull(url, onProgress) {
    const probe = await fetch(withRange(url, 0, 1), { credentials: 'include' });
    if (!probe.ok && probe.status !== 206) {
      throw new Error('HTTP ' + probe.status + '（链接可能已过期，重新播放一次）');
    }

    let total = 0;
    const cr = probe.headers.get('content-range');
    if (cr && cr.indexOf('/') >= 0) total = parseInt(cr.split('/')[1], 10) || 0;
    if (!total) {
      const cl = probe.headers.get('content-length');
      total = cl ? parseInt(cl, 10) : 0;
    }
    if (!total) throw new Error('无法确定流大小，可能链接已失效');

    const parts = [];
    let done = 0;
    for (let start = 0; start < total; start += CHUNK) {
      const end = Math.min(start + CHUNK - 1, total - 1);
      const res = await fetch(withRange(url, start, end), { credentials: 'include' });
      if (!res.ok && res.status !== 206) {
        throw new Error('下载中断：HTTP ' + res.status);
      }
      const buf = await res.arrayBuffer();
      parts.push(buf);
      done += buf.byteLength;
      if (onProgress) onProgress(Math.min(1, done / total), done, total);
    }

    return new Blob(parts, { type: 'video/mp4' });
  }

  global.YTDownload = { createMerger, fetchFull, withRange };
})(typeof self !== 'undefined' ? self : this);
