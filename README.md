# bili-audio-dl

Batch download audio from Bilibili (B站) user space. Pure Python 3.10+, zero dependencies.

## Features

- **Concurrent downloads** — parallel CDN downloads with thread pool
- **Checkpoint/resume** — survives blocks and restarts; just run the same command again
- **Cookie auth** — `--cookie SESSDATA=xxx` for much higher API quotas
- **Exponential backoff** — auto-backs off on rate-limit (412/352), doesn't blindly retry
- **Adaptive cooldown** — repeated blocks trigger longer waits and session rotation
- **Proxy support** — `--proxy socks5://127.0.0.1:1080` to distribute requests
- **Zero dependencies** — only Python standard library

## Install

```bash
git clone https://github.com/NeoXue-ai/bili-audio-dl.git
cd bili-audio-dl
```

## Usage

```bash
# Basic: download all audio from a user
python bili_audio_dl.py https://space.bilibili.com/2081722/video

# With login cookie (much higher API limits)
python bili_audio_dl.py 2081722 --cookie 'SESSDATA=your_sessdata_here'

# More workers for faster downloads
python bili_audio_dl.py 2081722 --workers 8

# Use proxy
python bili_audio_dl.py 2081722 --proxy socks5://127.0.0.1:1080

# Resume after interruption (auto-detects checkpoint)
python bili_audio_dl.py 2081722

# Only fetch video list
python bili_audio_dl.py 2081722 --list-only

# Read BV IDs from file
python bili_audio_dl.py 2081722 --from-file bvids.txt
```

## How to Get SESSDATA Cookie

1. Open Bilibili in your browser and log in
2. Open DevTools (F12) → Application → Cookies → `https://www.bilibili.com`
3. Copy the `SESSDATA` value
4. Use: `--cookie 'SESSDATA=abc123...'`

With SESSDATA, the API quota is significantly higher and you're much less likely to get blocked.

## How It Works

### Phase 1 — Resolve (API-bound, rate-limited)

For each video, calls the Bilibili API to get video info and audio stream URL:
- WBI signature on all API requests
- DM fingerprint args (`dm_img_str`, `dm_cover_img_str`, etc.) to bypass newer anti-scraping
- Token-bucket rate limiter caps API calls at ~2/sec
- Exponential backoff on rate-limit errors: 4s → 8s → 16s → 32s → 60s (max)
- After 3 consecutive blocks: auto-rotates session (new buvid + WBI keys)
- Resolved info is cached in `.checkpoint.json`

### Phase 2 — Download (CDN-bound, parallel)

Downloads audio files from Bilibili CDN using a thread pool:
- CDN endpoints are not rate-limited like the API
- Default 4 workers, use `--workers 8` for faster downloads
- Completed downloads are checkpointed immediately

### Checkpoint/Resume

Every resolved video and completed download is saved to `.checkpoint.json`. If the process is interrupted (Ctrl+C, network error, rate-limit block), just run the same command again:

```bash
# First run: downloads 200/360, gets blocked
python bili_audio_dl.py 2081722

# Second run: skips 200 already done, continues from #201
python bili_audio_dl.py 2081722
```

## Output Structure

```
bilibili_audio/
├── Video Title 1.m4a
├── Video Title 2.m4a
├── ...
├── bvids.txt           # All BV IDs
├── failed.txt          # Failed downloads (if any)
└── .checkpoint.json    # Resume checkpoint (auto-managed)
```

## Requirements

- Python 3.10+
- No third-party packages

## License

MIT
