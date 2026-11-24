"""本地 HTTP/HTTPS 代理（MITM）。

为什么必须 MITM：视频站的流地址几乎全在 HTTPS 里，不解密就只能看到域名，
看不到 m3u8 / mp4 的具体路径。代理用自己的 CA 给每个域名现签证书，
浏览器信任这张 CA 之后，明文请求就流经这里了。

设计取舍：
- 每个请求单独建上游连接（不复用连接池），换来实现简单可靠；
- 响应体一律流式转发，绝不整块读进内存，否则下 4K 视频会直接爆；
- 只对 HTTP(S) 生效，QUIC/HTTP3 走 UDP，代理看不到（README 里说明了怎么关）。
"""

from __future__ import annotations

import socket
import ssl
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

from certs import CertManager, CertError
from sniffer import classify, is_candidate, dedupe_key, filename_for

HOP_HEADERS = {
    "proxy-connection",
    "proxy-authenticate",
    "proxy-authorization",
    "connection",
    "keep-alive",
}


@dataclass
class Request:
    method: str
    target: str
    version: str
    headers: list[tuple[str, str]] = field(default_factory=list)

    def get(self, name: str, default: str = "") -> str:
        low = name.lower()
        for k, v in self.headers:
            if k.lower() == low:
                return v
        return default

    def header_map(self) -> dict[str, str]:
        return {k.lower(): v for k, v in self.headers}


@dataclass
class MediaHit:
    url: str
    kind: str
    mime: str
    size: int
    referer: str
    user_agent: str
    origin: str
    filename: str
    at: float = field(default_factory=time.time)


class SockReader:
    """带缓冲的 socket 读取器，避免把 body 读过头。"""

    def __init__(self, sock: socket.socket):
        self.sock = sock
        self.buf = b""

    def read_until(self, sep: bytes = b"\r\n\r\n", limit: int = 256 * 1024) -> bytes | None:
        while sep not in self.buf:
            chunk = self.sock.recv(16384)
            if not chunk:
                return None
            self.buf += chunk
            if len(self.buf) > limit:
                raise ValueError("header too large")
        idx = self.buf.index(sep) + len(sep)
        head, self.buf = self.buf[:idx], self.buf[idx:]
        return head

    def read_exact(self, n: int) -> bytes:
        while len(self.buf) < n:
            chunk = self.sock.recv(min(65536, n - len(self.buf)))
            if not chunk:
                break
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def read_line(self) -> bytes:
        while b"\r\n" not in self.buf:
            chunk = self.sock.recv(8192)
            if not chunk:
                break
            self.buf += chunk
        idx = self.buf.find(b"\r\n")
        if idx < 0:
            out, self.buf = self.buf, b""
            return out
        out, self.buf = self.buf[:idx], self.buf[idx + 2:]
        return out

    def read_some(self) -> bytes:
        if self.buf:
            out, self.buf = self.buf, b""
            return out
        return self.sock.recv(65536)


def parse_head(raw: bytes) -> Request | None:
    try:
        text = raw.decode("latin-1")
    except Exception:
        return None
    lines = text.split("\r\n")
    if not lines or not lines[0]:
        return None
    parts = lines[0].split(" ")
    if len(parts) < 2:
        return None
    method, target = parts[0], parts[1]
    version = parts[2] if len(parts) > 2 else "HTTP/1.1"
    headers = []
    for line in lines[1:]:
        if not line or ":" not in line:
            continue
        k, v = line.split(":", 1)
        headers.append((k.strip(), v.strip()))
    return Request(method, target, version, headers)


def parse_response_head(raw: bytes) -> tuple[int, list[tuple[str, str]]]:
    text = raw.decode("latin-1")
    lines = text.split("\r\n")
    status = 0
    if lines:
        p = lines[0].split(" ")
        if len(p) >= 2 and p[1].isdigit():
            status = int(p[1])
    headers = []
    for line in lines[1:]:
        if not line or ":" not in line:
            continue
        k, v = line.split(":", 1)
        headers.append((k.strip(), v.strip()))
    return status, headers


def header_value(headers: list[tuple[str, str]], name: str) -> str:
    low = name.lower()
    for k, v in headers:
        if k.lower() == low:
            return v
    return ""


def rebuild_head(req: Request, path: str, extra: dict[str, str] | None = None) -> bytes:
    lines = [f"{req.method} {path} {req.version}"]
    for k, v in req.headers:
        if k.lower() in HOP_HEADERS:
            continue
        lines.append(f"{k}: {v}")
    if extra:
        for k, v in extra.items():
            lines.append(f"{k}: {v}")
    lines.append("Connection: close")
    return ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1")


class ProxyServer:
    def __init__(
        self,
        certs: CertManager,
        on_media: Callable[[MediaHit], None] | None = None,
        host: str = "127.0.0.1",
        port: int = 8899,
    ):
        self.certs = certs
        self.on_media = on_media
        self.host = host
        self.port = port
        self._sock: socket.socket | None = None
        self._running = False
        self._thread: threading.Thread | None = None
        self._ctx_cache: dict[str, ssl.SSLContext] = {}
        self._ctx_lock = threading.Lock()
        self.stats = {"requests": 0, "media": 0, "errors": 0}

    # ---------------- 生命周期 ----------------

    def start(self) -> None:
        if self._running:
            return
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((self.host, self.port))
        s.listen(128)
        s.settimeout(1.0)
        self._sock = s
        self._running = True
        self._thread = threading.Thread(target=self._accept_loop, daemon=True, name="proxy-accept")
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        if self._sock:
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None

    @property
    def running(self) -> bool:
        return self._running

    def _accept_loop(self) -> None:
        while self._running and self._sock:
            try:
                conn, _addr = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            conn.settimeout(60)
            threading.Thread(target=self._handle_client, args=(conn,), daemon=True).start()

    # ---------------- 连接处理 ----------------

    def _handle_client(self, conn: socket.socket) -> None:
        reader = SockReader(conn)
        try:
            head = reader.read_until()
            if not head:
                return
            req = parse_head(head)
            if not req:
                return

            if req.method.upper() == "CONNECT":
                self._do_connect(conn, reader, req)
            else:
                self._do_plain(conn, reader, req)
        except (ConnectionError, socket.timeout, ssl.SSLError):
            pass
        except Exception:
            self.stats["errors"] += 1
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def _do_connect(self, conn: socket.socket, reader: SockReader, req: Request) -> None:
        target = req.target
        if ":" in target:
            host, port_s = target.rsplit(":", 1)
            try:
                port = int(port_s)
            except ValueError:
                port = 443
        else:
            host, port = target, 443

        try:
            crt, key = self.certs.site_cert(host)
        except CertError:
            conn.sendall(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
            return

        conn.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")

        ctx = self._context_for(host, crt, key)
        if ctx is None:
            return
        try:
            tls = ctx.wrap_socket(conn, server_side=True)
        except (ssl.SSLError, OSError):
            return

        tls.settimeout(60)
        tls_reader = SockReader(tls)
        try:
            self._serve_http(tls, tls_reader, "https", host, port)
        finally:
            try:
                tls.close()
            except Exception:
                pass

    def _context_for(self, host: str, crt: str, key: str) -> ssl.SSLContext | None:
        with self._ctx_lock:
            ctx = self._ctx_cache.get(host)
            if ctx:
                return ctx
            try:
                ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
                ctx.load_cert_chain(crt, key)
                # 兼容老客户端
                ctx.minimum_version = ssl.TLSVersion.TLSv1_2
                self._ctx_cache[host] = ctx
                return ctx
            except Exception:
                return None

    def _do_plain(self, conn: socket.socket, reader: SockReader, req: Request) -> None:
        # 普通 HTTP 代理请求，target 是绝对 URI
        self._serve_http(conn, reader, "http", "", 80, first=req)

    def _serve_http(
        self,
        client: socket.socket,
        reader: SockReader,
        scheme: str,
        default_host: str,
        default_port: int,
        first: Request | None = None,
    ) -> None:
        req = first
        while True:
            if req is None:
                head = reader.read_until()
                if not head:
                    return
                req = parse_head(head)
                if not req:
                    return

            try:
                self._forward(client, reader, req, scheme, default_host, default_port)
            except (ConnectionError, socket.timeout, ssl.SSLError):
                return
            except Exception:
                self.stats["errors"] += 1
                return

            # 上游一律 Connection: close，所以一个请求结束就收工
            return

    def _forward(
        self,
        client: socket.socket,
        reader: SockReader,
        req: Request,
        scheme: str,
        default_host: str,
        default_port: int,
    ) -> None:
        target = req.target
        if target.startswith("http://") or target.startswith("https://"):
            from urllib.parse import urlsplit

            p = urlsplit(target)
            host, port, path = p.hostname or "", p.port or (443 if p.scheme == "https" else 80), p.path or "/"
            if p.query:
                path += "?" + p.query
            scheme = p.scheme
        else:
            host, port, path = default_host, default_port, target

        if not host:
            return

        self.stats["requests"] += 1
        url = f"{scheme}://{host}{path}"

        # WebSocket 之类的升级请求，直接双向管道透传
        if req.get("upgrade"):
            self._tunnel_upgrade(client, reader, req, host, port, path)
            return

        try:
            raw_up = socket.create_connection((host, port), timeout=30)
            if scheme == "https":
                # 上游是 TLS 端口，解密之后必须重新加密才能跟它说话
                up_ctx = ssl.create_default_context()
                up_ctx.check_hostname = False
                up_ctx.verify_mode = ssl.CERT_NONE
                upstream = up_ctx.wrap_socket(raw_up, server_hostname=host)
            else:
                upstream = raw_up
        except (OSError, ssl.SSLError):
            client.sendall(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
            return

        try:
            upstream.sendall(rebuild_head(req, path))
            up_reader = SockReader(upstream)
            raw_resp = up_reader.read_until()
            if not raw_resp:
                client.sendall(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
                return

            status, resp_headers = parse_response_head(raw_resp)

            # 1xx 中间响应：转发后继续等真正的响应
            while 100 <= status < 200:
                client.sendall(raw_resp)
                raw_resp = up_reader.read_until()
                if not raw_resp:
                    return
                status, resp_headers = parse_response_head(raw_resp)

            self._maybe_emit(url, req, resp_headers, status)

            client.sendall(raw_resp)
            self._stream_body(up_reader, client, resp_headers, status, req.method)
        finally:
            try:
                upstream.close()
            except Exception:
                pass

    def _tunnel_upgrade(
        self,
        client: socket.socket,
        reader: SockReader,
        req: Request,
        host: str,
        port: int,
        path: str,
    ) -> None:
        upstream = socket.create_connection((host, port), timeout=30)
        try:
            upstream.sendall(rebuild_head(req, path))
            if reader.buf:
                upstream.sendall(reader.buf)
                reader.buf = b""

            def pump(src: socket.socket, dst: socket.socket) -> None:
                try:
                    while True:
                        chunk = src.recv(65536)
                        if not chunk:
                            break
                        dst.sendall(chunk)
                except Exception:
                    pass

            t = threading.Thread(target=pump, args=(upstream, client), daemon=True)
            t.start()
            pump(client, upstream)
            t.join(timeout=5)
        finally:
            try:
                upstream.close()
            except Exception:
                pass

    # ---------------- 响应体流式转发 ----------------

    def _stream_body(
        self,
        src: SockReader,
        dst: socket.socket,
        headers: list[tuple[str, str]],
        status: int,
        method: str,
    ) -> None:
        if method.upper() == "HEAD" or status in (204, 304):
            return

        te = header_value(headers, "transfer-encoding").lower()
        cl_raw = header_value(headers, "content-length")

        if "chunked" in te:
            self._stream_chunked(src, dst)
            return

        if cl_raw.isdigit():
            remain = int(cl_raw)
            while remain > 0:
                chunk = src.read_some()
                if not chunk:
                    return
                if len(chunk) > remain:
                    dst.sendall(chunk[:remain])
                    return
                dst.sendall(chunk)
                remain -= len(chunk)
            return

        # 既没有长度也不是 chunked：读到对端关闭
        while True:
            chunk = src.read_some()
            if not chunk:
                return
            dst.sendall(chunk)

    def _stream_chunked(self, src: SockReader, dst: socket.socket) -> None:
        while True:
            line = src.read_line()
            if not line:
                return
            dst.sendall(line + b"\r\n")
            try:
                size = int(line.split(b";")[0].strip(), 16)
            except ValueError:
                return
            if size == 0:
                # 读掉 trailer
                while True:
                    t = src.read_line()
                    if not t:
                        return
                    dst.sendall(t + b"\r\n")
                    if t == b"":
                        return
            data = src.read_exact(size)
            if len(data) < size:
                dst.sendall(data)
                return
            dst.sendall(data)
            src.read_exact(2)  # CRLF
            dst.sendall(b"\r\n")

    # ---------------- 嗅探 ----------------

    def _maybe_emit(
        self,
        url: str,
        req: Request,
        resp_headers: list[tuple[str, str]],
        status: int,
    ) -> None:
        if not self.on_media or status >= 400:
            return

        mime = header_value(resp_headers, "content-type").split(";")[0].strip()
        size_raw = header_value(resp_headers, "content-length")
        size = int(size_raw) if size_raw.isdigit() else 0

        kind = classify(url, mime, size)
        if not is_candidate(kind, url, mime, size):
            return

        req_headers = req.header_map()
        referer = req_headers.get("referer", "")
        ua = req_headers.get("user-agent", "")

        from urllib.parse import urlsplit

        p = urlsplit(url)
        origin = f"{p.scheme}://{p.netloc}"

        hit = MediaHit(
            url=url,
            kind=kind,
            mime=mime,
            size=size,
            referer=referer,
            user_agent=ua,
            origin=origin,
            filename=filename_for(url, kind),
        )
        self.stats["media"] += 1
        try:
            self.on_media(hit)
        except Exception:
            pass
