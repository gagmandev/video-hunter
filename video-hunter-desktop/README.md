# Video Hunter Desktop

一个 Windows 桌面端的视频嗅探下载器。思路和 IDM 一样：**在本机起一个代理，
所有走代理的流量都从它这里过，看到视频流就记下来，然后多线程分段下载。**

和浏览器扩展的区别：扩展只能看到浏览器自己发出的请求，而且受 CORS、
扩展沙箱限制。桌面客户端走系统代理，**浏览器、桌面播放器、客户端程序
（B 站客户端、抖音客户端等）的流量都能覆盖**——只要它走 HTTP(S) 代理。

## 它做什么

| 能力 | 说明 |
| --- | --- |
| HTTPS 解密 | 本地 CA 给每个域名现签证书，MITM 之后能看到完整的 m3u8 / mp4 地址 |
| 多线程下载 | HTTP Range 切段并发，默认 8 路，可调 |
| 断点续传 | 每段进度写进 `.vhmeta`，进程重启后接着下 |
| HLS 合并 | 解析 m3u8、并发拉分片、AES-128 解密、按序拼接 |
| 自动嗅探 | MP4 / WebM / FLV 直链、HLS、DASH、YouTube、B 站 CDN |
| 系统代理一键开关 | 改注册表 + 通知 WinINet 刷新，立刻生效 |
| Web 控制台 | 浏览器打开就是界面，不依赖 tkinter / Qt |

## 快速开始

```bat
:: 1. 安装根证书（只需要一次）
install_ca.bat

:: 2. 启动
run.bat
```

启动后会自动打开 `http://127.0.0.1:8900/`。点右上角 **开启系统代理**，
然后在浏览器里打开视频页播放，列表里就会出现流地址。

命令行参数：

```bat
python main.py --port 8899 --ui-port 8900 --dest D:\videos --threads 8 --auto-proxy
```

## 安装证书（重要）

不装证书的话，HTTPS 站点会报 `NET::ERR_CERT_AUTHORITY_INVALID`，代理也看不到流地址。

**方式一（推荐）**：双击 `install_ca.bat`，装到当前用户的「受信任的根证书颁发机构」。

**方式二（手动）**：

1. 启动一次程序，生成 `certs\ca.crt`
2. 双击 `certs\ca.crt` → 安装证书 → 当前用户 → 受信任的根证书颁发机构
3. 重启浏览器

**方式三（Firefox）**：Firefox 用自己的证书库。设置 → 隐私与安全 →
证书 → 查看证书 → 证书颁发机构 → 导入 → 选 `certs\ca.crt`，勾选「信任由此证书颁发机构来标识网站」。

> 私钥在 `certs\ca.key`，只存在本机。这张 CA 只该用来给本机代理签证书，
> 不要拷到别的机器上。不想用了：`certmgr.msc` → 受信任的根证书颁发机构 → 删除
> "VideoHunter Local CA"。

## 关闭 QUIC / HTTP3

Chrome / Edge 默认会尝试 QUIC（UDP 443）。**UDP 不走 HTTP 代理**，这部分流量
代理看不到，表现就是"有些站抓不到"。

在 `chrome://flags` 里把 **Experimental QUIC protocol** 设为 `Disabled`，重启浏览器。

Firefox 默认不开 HTTP/3，一般不用管。

## 能下什么、下不了什么

**能下**

- MP4 / WebM / FLV 直链
- HLS（m3u8），含 AES-128 加密
- fMP4 切片（`#EXT-X-MAP`）
- B 站（DASH 音视频分离，需要另外合并，见下）

**下不了**

| 类型 | 原因 |
| --- | --- |
| Widevine / PlayReady / FairPlay | 密钥在 CDM 或 TEE 里，MITM 只能拿到密文 |
| SAMPLE-AES 加密的 HLS | 同上，属于 DRM |
| DASH（`.mpd`） | 客户端目前没做 DASH 解析，用 `yt-dlp` |
| 证书固定（pinning）的 App | MITM 握手会被客户端拒绝 |
| 非 HTTP 协议（RTMP / 私有协议） | 代理层看不到 |

## 和 IDM 的差距

老实说，这个工具覆盖的是 IDM 最核心的那部分，不是全部。没做的：

- **浏览器下载接管**：IDM 靠浏览器扩展 + Native Messaging 把下载转给主程序。
  这里需要另外写一个 native host 注册脚本，暂未包含。
- **站点抓取器**：按规则批量抓整个站点的资源。
- **计划任务 / 队列调度 / 限速**。
- **FTP / 金属链接**。
- **DASH 合并**：`.mpd` 的解析和音视频重封装还没做，这类用 `yt-dlp` 更省事。

## 目录结构

```
video-hunter-desktop/
  main.py          入口，串起代理 + 控制台 + 任务
  proxy.py         HTTP/HTTPS MITM 代理
  certs.py         本地 CA 与站点证书（openssl）
  sniffer.py       媒体类型识别
  downloader.py    多线程分段下载 + 断点续传
  hlsdl.py         HLS 解析 / 并发分片 / AES 解密
  aes.py           纯 Python AES-128-CBC
  systemproxy.py   Windows 系统代理开关
  store.py         状态与配置
  ui.py            Web 控制台（内嵌页面）
  run.bat          启动
  install_ca.bat   安装根证书
```

## 依赖

- Windows 10/11
- Python 3.10+
- **openssl**（Git for Windows 自带；没装 Git 的话需要单独装一个）

没有第三方 Python 包，全部标准库。

## 已验证

端到端测试 13 项全过：

- CA 与站点证书签发
- HTTP 直链嗅探、m3u8 识别
- 4 线程分段下载，1MB 文件逐字节比对一致
- Range 探测（大小 + 是否支持分段）
- HLS 3 分片合并，内容一致
- HLS AES-128 解密（openssl 加密 → 本程序解密），内容一致
- HTTPS MITM：CONNECT 隧道、证书替换、内容正常返回、请求被嗅探

## 已知问题

- **每个请求单独建上游连接**，没有连接池。并发抓很多小分片时会慢一些，
  但正确性优先。
- **纯 Python AES 解密**大概 2-4 MB/s，超长加密 HLS 会明显慢于 ffmpeg。
- 代理默认只监听 `127.0.0.1`，不对外网开放。
- 退出时如果 `auto_proxy` 开着，会自动关掉系统代理；异常退出可能残留，
  手动去「设置 → 网络 → 代理」关一下即可。
