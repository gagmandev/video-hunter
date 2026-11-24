"""本地 CA 与站点证书管理。

MITM 代理需要为每个访问的域名现场签发一张证书，浏览器才肯接受。
这里用 openssl 命令行完成（Windows 上 Git for Windows 自带），
签好的证书按域名缓存，之后复用，避免每次都付进程启动开销。
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

CA_CN = "VideoHunter Local CA"
CA_ORG = "VideoHunter"
CA_DAYS = 3650
SITE_DAYS = 365
# 缓存有效期，到期前重新签，避免长跑之后证书过期
SITE_REFRESH_SECONDS = 300 * 24 * 3600


def _candidate_openssl() -> list[str]:
    cands = [
        shutil.which("openssl"),
        r"C:\Program Files\Git\usr\bin\openssl.exe",
        r"C:\Program Files (x86)\Git\usr\bin\openssl.exe",
        "/usr/bin/openssl",
        "/usr/local/bin/openssl",
    ]
    out = []
    for c in cands:
        if c and os.path.isfile(c) and c not in out:
            out.append(c)
    return out


class CertError(RuntimeError):
    pass


class CertManager:
    def __init__(self, base_dir: str | Path):
        self.base = Path(base_dir)
        self.dir = self.base / "certs"
        self.sites_dir = self.dir / "sites"
        self.sites_dir.mkdir(parents=True, exist_ok=True)

        self.ca_key = self.dir / "ca.key"
        self.ca_crt = self.dir / "ca.crt"
        self.ca_srl = self.dir / "ca.srl"

        self.openssl = None
        for c in _candidate_openssl():
            try:
                r = subprocess.run([c, "version"], capture_output=True, text=True, timeout=10)
                if r.returncode == 0 and "OpenSSL" in (r.stdout or ""):
                    self.openssl = c
                    break
            except Exception:
                continue
        if not self.openssl:
            raise CertError(
                "找不到 openssl。请安装 Git for Windows（自带 openssl），"
                "或把 openssl.exe 放到 PATH 里。"
            )

        self._lock = threading.Lock()
        self._ensure_ca()

    # ---------------- 内部 ----------------

    def _run(self, args: list[str]) -> subprocess.CompletedProcess:
        return subprocess.run(
            [self.openssl, *args],
            capture_output=True,
            text=True,
            timeout=60,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )

    def _ensure_ca(self) -> None:
        if self.ca_key.exists() and self.ca_crt.exists():
            return
        with self._lock:
            if self.ca_key.exists() and self.ca_crt.exists():
                return
            r = self._run(
                [
                    "req", "-x509", "-newkey", "rsa:2048",
                    "-keyout", str(self.ca_key),
                    "-out", str(self.ca_crt),
                    "-days", str(CA_DAYS),
                    "-nodes",
                    "-subj", f"/CN={CA_CN}/O={CA_ORG}",
                    "-addext", "basicConstraints=critical,CA:TRUE,pathlen:0",
                    "-addext", "keyUsage=critical,keyCertSign,cRLSign",
                ]
            )
            if r.returncode != 0 or not self.ca_crt.exists():
                raise CertError("生成根证书失败：" + (r.stderr or r.stdout or "")[:300])

    @staticmethod
    def _safe_name(host: str) -> str:
        return re.sub(r"[^A-Za-z0-9._-]", "_", host)[:120]

    def _paths(self, host: str) -> tuple[Path, Path]:
        name = self._safe_name(host)
        return self.sites_dir / f"{name}.crt", self.sites_dir / f"{name}.key"

    # ---------------- 对外 ----------------

    @property
    def ca_path(self) -> str:
        return str(self.ca_crt)

    def ca_fingerprint(self) -> str:
        """返回 SHA-1 指纹，方便用户核对装的是不是这一张。"""
        r = self._run(["x509", "-in", str(self.ca_crt), "-noout", "-fingerprint", "-sha1"])
        line = (r.stdout or "").strip()
        return line.split("=", 1)[-1] if "=" in line else line

    def site_cert(self, host: str) -> tuple[str, str]:
        """返回 (证书路径, 私钥路径)，必要时现场签发。"""
        host = (host or "").strip().lower()
        if not host:
            raise CertError("空域名")

        crt, key = self._paths(host)
        csr = crt.with_suffix(".csr")
        ext = crt.with_suffix(".ext")
        if crt.exists() and key.exists():
            if time.time() - crt.stat().st_mtime < SITE_REFRESH_SECONDS:
                return str(crt), str(key)

        with self._lock:
            if crt.exists() and key.exists():
                if time.time() - crt.stat().st_mtime < SITE_REFRESH_SECONDS:
                    return str(crt), str(key)

            # IP 直连要写成 IP: 而不是 DNS:
            san = f"IP:{host}" if re.fullmatch(r"[\d.]+", host) else f"DNS:{host}"
            # 主域带一个通配符，子域复用同一张
            if not re.fullmatch(r"[\d.]+", host):
                parts = host.split(".")
                if len(parts) >= 2:
                    san += f",DNS:*.{'.'.join(parts[-2:])}"

            r = self._run(
                [
                    "req", "-new", "-newkey", "rsa:2048",
                    "-keyout", str(key),
                    "-out", str(csr),
                    "-nodes",
                    "-subj", f"/CN={host}",
                    "-addext", f"subjectAltName={san}",
                ]
            )
            if r.returncode != 0 or not csr.exists():
                raise CertError(f"生成 {host} 证书请求失败：" + (r.stderr or r.stdout or "")[:300])

            ext.write_text(
                "subjectAltName=" + san + "\n"
                "basicConstraints=CA:FALSE\n"
                "keyUsage=digitalSignature,keyEncipherment\n"
                "extendedKeyUsage=serverAuth\n",
                encoding="utf-8",
            )

            r = self._run(
                [
                    "x509", "-req",
                    "-in", str(csr),
                    "-CA", str(self.ca_crt),
                    "-CAkey", str(self.ca_key),
                    "-CAcreateserial",
                    "-out", str(crt),
                    "-days", str(SITE_DAYS),
                    "-extfile", str(ext),
                ]
            )
            for tmp_file in (csr, ext):
                try:
                    tmp_file.unlink()
                except Exception:
                    pass

            if r.returncode != 0 or not crt.exists():
                raise CertError(f"签发 {host} 证书失败：" + (r.stderr or r.stdout or "")[:300])
            return str(crt), str(key)
