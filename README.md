# bili-audio-dl

[English](#english) | [中文](#中文)

---

## English

Batch download audio from Bilibili (B站) user space. Pure Python 3.10+, zero dependencies.

### Features

- **Concurrent downloads** — parallel CDN downloads with configurable thread pool
- **Checkpoint/resume** — survives rate-limit blocks and interruptions; just rerun the same command
- **Anti-scraping bypass** — WBI signature, DM fingerprint, WebPage fallback
- **Cookie auth** — `--cookie SESSDATA=xxx` for much higher API quotas
- **Exponential backoff** — auto-backs off on 412/352 errors with jitter
- **Session rotation** — auto-rotates device fingerprint after repeated blocks
- **Proxy support** — `--proxy socks5://127.0.0.1:1080`
- **Zero dependencies** — only Python standard library

### Install

```bash
git clone https://github.com/NeoXue-ai/bili-audio-dl.git
cd bili-audio-dl
```

### Quick Start

> **Recommend using SESSDATA cookie** — without it, API quota is very low and you'll get blocked frequently. With SESSDATA, downloads are **2-3x faster**. See [How to Get SESSDATA](#how-to-get-sessdata-cookie) below.

```bash
# Recommended: download with login cookie
python bili_audio_dl.py USER_MID --cookie 'SESSDATA=your_sessdata_here'

# Without cookie (slower, more likely to be blocked)
python bili_audio_dl.py https://space.bilibili.com/USER_MID/video

# More workers for faster downloads
python bili_audio_dl.py USER_MID --cookie 'SESSDATA=xxx' --workers 8

# Use proxy
python bili_audio_dl.py USER_MID --proxy socks5://127.0.0.1:1080

# Resume after interruption (auto-detects checkpoint)
python bili_audio_dl.py USER_MID

# Only fetch video list, don't download
python bili_audio_dl.py USER_MID --list-only

# Read BV IDs from file
python bili_audio_dl.py USER_MID --from-file bvids.txt
```

### How to Get SESSDATA Cookie

1. Open Bilibili in your browser and log in
2. Open DevTools (F12) → Application → Cookies → `https://www.bilibili.com`
3. Copy the `SESSDATA` value
4. Use: `--cookie 'SESSDATA=abc123...'`

With SESSDATA, the API quota is significantly higher and you're much less likely to get blocked.

### How It Works

**Phase 1 — Resolve** (API-bound, rate-limited)

For each video, calls the Bilibili API to get video info and audio stream URL:
- WBI signature on all API requests
- DM fingerprint args to bypass anti-scraping detection
- WebPage fallback: if API fails, extracts `__playinfo__` from video page HTML
- Token-bucket rate limiter caps API calls at ~2/sec
- Exponential backoff on rate-limit errors: 4s → 8s → 16s → 32s → 60s (max)
- After 3 consecutive blocks: auto-rotates session (new buvid + WBI keys)
- Resolved info is cached in `.checkpoint.json`

**Phase 2 — Download** (CDN-bound, parallel)

Downloads audio files from Bilibili CDN using a thread pool:
- CDN endpoints are not rate-limited like the API
- Default 4 workers, use `--workers 8` for faster downloads
- Completed downloads are checkpointed immediately

**Checkpoint/Resume**

Every resolved video and completed download is saved to `.checkpoint.json`. If interrupted (Ctrl+C, network error, rate-limit block), just rerun:

```bash
# First run: downloads 200/360, gets blocked
python bili_audio_dl.py USER_MID

# Second run: skips 200 already done, continues from #201
python bili_audio_dl.py USER_MID
```

### Output

```
bilibili_audio/
├── Video Title 1.m4a
├── Video Title 2.m4a
├── ...
├── bvids.txt           # All BV IDs
├── failed.txt          # Failed downloads (if any)
└── .checkpoint.json    # Resume checkpoint (auto-managed)
```

### Requirements

- Python 3.10+
- No third-party packages

### License

MIT

---

## 中文

批量下载 Bilibili 用户空间的音频。纯 Python 3.10+，零依赖。

### 功能特性

- **并发下载** — 线程池并行下载 CDN 资源
- **断点续传** — 被封控或中断后，重新运行同一命令即可继续
- **反爬绕过** — WBI 签名、DM 指纹、WebPage 兜底
- **Cookie 认证** — `--cookie SESSDATA=xxx` 大幅提高 API 配额
- **指数退避** — 遇到 412/352 错误自动退避，带随机抖动
- **会话轮换** — 连续被封后自动刷新设备指纹
- **代理支持** — `--proxy socks5://127.0.0.1:1080`
- **零依赖** — 仅使用 Python 标准库

### 安装

```bash
git clone https://github.com/NeoXue-ai/bili-audio-dl.git
cd bili-audio-dl
```

### 快速开始

> **建议配置 SESSDATA Cookie** — 不配置时 API 配额很低，容易被封控。配置后下载速度提升 **2-3 倍**。获取方式见下方 [如何获取 SESSDATA](#如何获取-sessdata-cookie)。

```bash
# 推荐：使用登录 Cookie 下载
python bili_audio_dl.py USER_MID --cookie 'SESSDATA=你的sessdata'

# 不用 Cookie（更慢，更容易被封）
python bili_audio_dl.py https://space.bilibili.com/USER_MID/video

# 增加下载并发数
python bili_audio_dl.py USER_MID --cookie 'SESSDATA=xxx' --workers 8

# 使用代理
python bili_audio_dl.py USER_MID --proxy socks5://127.0.0.1:1080

# 中断后恢复（自动检测断点）
python bili_audio_dl.py USER_MID

# 仅获取视频列表，不下载
python bili_audio_dl.py USER_MID --list-only

# 从文件读取 BV 号
python bili_audio_dl.py USER_MID --from-file bvids.txt
```

### 如何获取 SESSDATA Cookie

1. 在浏览器中打开 Bilibili 并登录
2. 打开开发者工具 (F12) → Application → Cookies → `https://www.bilibili.com`
3. 复制 `SESSDATA` 的值
4. 使用：`--cookie 'SESSDATA=abc123...'`

使用 SESSDATA 后，API 配额显著提高，被封控的概率大大降低。

### 工作原理

**阶段一 — 解析**（API 请求，有频率限制）

对每个视频调用 Bilibili API 获取视频信息和音频流地址：
- 所有 API 请求带 WBI 签名
- DM 指纹参数绕过反爬检测
- WebPage 兜底：API 失败时，从视频页面 HTML 提取 `__playinfo__`
- 令牌桶限速器将 API 调用限制在 ~2次/秒
- 遇到限频错误指数退避：4s → 8s → 16s → 32s → 60s（上限）
- 连续被封 3 次后自动轮换会话（新 buvid + WBI 密钥）
- 已解析信息缓存到 `.checkpoint.json`

**阶段二 — 下载**（CDN 请求，并行）

使用线程池从 Bilibili CDN 并行下载音频：
- CDN 端点不受 API 频率限制
- 默认 4 个并发，可用 `--workers 8` 加速
- 每个文件下载完成立即记录断点

**断点续传**

每个已解析和已下载的视频都保存到 `.checkpoint.json`。如果被中断（Ctrl+C、网络错误、封控），重新运行即可：

```bash
# 第一次运行：下载了 200/360，被封控
python bili_audio_dl.py USER_MID

# 第二次运行：跳过已完成的 200 个，从 #201 继续
python bili_audio_dl.py USER_MID
```

### 输出结构

```
bilibili_audio/
├── 视频标题1.m4a
├── 视频标题2.m4a
├── ...
├── bvids.txt           # 所有 BV 号
├── failed.txt          # 下载失败的（如有）
└── .checkpoint.json    # 断点文件（自动管理）
```

### 环境要求

- Python 3.10+
- 无需安装第三方包

### 许可证

MIT
