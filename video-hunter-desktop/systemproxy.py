"""Windows 系统代理开关。

改注册表只是第一步，WinINet 有缓存，必须调 InternetSetOption 通知系统刷新，
否则浏览器要等一会儿才认。这两步都做了才算真正生效。
"""

from __future__ import annotations

import ctypes
import sys

INTERNET_OPTION_SETTINGS_CHANGED = 39
INTERNET_OPTION_REFRESH = 37

REG_PATH = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"


def _refresh() -> None:
    try:
        wininet = ctypes.windll.Wininet
        wininet.InternetSetOptionW(0, INTERNET_OPTION_SETTINGS_CHANGED, None, 0)
        wininet.InternetSetOptionW(0, INTERNET_OPTION_REFRESH, None, 0)
    except Exception:
        pass


def is_supported() -> bool:
    return sys.platform == "win32"


def enable(host: str = "127.0.0.1", port: int = 8899, bypass: str = "<local>") -> tuple[bool, str]:
    if not is_supported():
        return False, "当前系统不支持自动设置代理（仅 Windows）"
    try:
        import winreg

        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, REG_PATH, 0, winreg.KEY_ALL_ACCESS)
        try:
            winreg.SetValueEx(key, "ProxyEnable", 0, winreg.REG_DWORD, 1)
            winreg.SetValueEx(key, "ProxyServer", 0, winreg.REG_SZ, f"{host}:{port}")
            winreg.SetValueEx(key, "ProxyOverride", 0, winreg.REG_SZ, bypass)
        finally:
            winreg.CloseKey(key)
        _refresh()
        return True, f"系统代理已指向 {host}:{port}"
    except Exception as e:
        return False, f"设置失败：{e}"


def disable() -> tuple[bool, str]:
    if not is_supported():
        return False, "当前系统不支持自动设置代理"
    try:
        import winreg

        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, REG_PATH, 0, winreg.KEY_ALL_ACCESS)
        try:
            winreg.SetValueEx(key, "ProxyEnable", 0, winreg.REG_DWORD, 0)
        finally:
            winreg.CloseKey(key)
        _refresh()
        return True, "系统代理已关闭"
    except Exception as e:
        return False, f"关闭失败：{e}"


def current() -> dict:
    if not is_supported():
        return {"enabled": False, "server": "", "supported": False}
    try:
        import winreg

        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, REG_PATH, 0, winreg.KEY_READ)
        try:
            try:
                enabled, _ = winreg.QueryValueEx(key, "ProxyEnable")
            except FileNotFoundError:
                enabled = 0
            try:
                server, _ = winreg.QueryValueEx(key, "ProxyServer")
            except FileNotFoundError:
                server = ""
        finally:
            winreg.CloseKey(key)
        return {"enabled": bool(enabled), "server": server, "supported": True}
    except Exception:
        return {"enabled": False, "server": "", "supported": True}
