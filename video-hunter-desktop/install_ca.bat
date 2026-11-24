@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo ============================================================
echo   安装 Video Hunter 本地根证书
echo ============================================================
echo.
echo 这张证书只用来解密本机代理的 HTTPS 流量，
echo 私钥保存在本目录 certs\ca.key，不会上传到任何地方。
echo.
echo 装到「当前用户」的受信任根证书区，不需要管理员权限。
echo.

if not exist "certs\ca.crt" (
  echo 首次使用，先生成证书...
  python -c "from certs import CertManager; from pathlib import Path; c = CertManager(Path('.')); print(c.ca_path)"
  if errorlevel 1 (
    echo [错误] 生成证书失败，请确认已安装 Python 和 Git for Windows
    pause
    exit /b 1
  )
)

certutil -addstore -user Root "certs\ca.crt"
if errorlevel 1 (
  echo.
  echo [失败] 证书安装失败。可以手动操作：
  echo   1. 双击 certs\ca.crt
  echo   2. 安装证书 - 当前用户 - 受信任的根证书颁发机构
  pause
  exit /b 1
)

echo.
echo [完成] 证书已安装。重启浏览器后生效。
echo.
pause
