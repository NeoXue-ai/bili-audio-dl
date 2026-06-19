# bili-audio-dl

Batch download audio from Bilibili (B站) user space. Pure Python 3.10+, zero dependencies.

## Features

- **Concurrent downloads** — parallel workers for fast throughput
- **Two-phase pipeline** — API resolution (rate-limited) + CDN download (parallel)
- **Token-bucket rate limiter** — respects Bilibili's API limits without unnecessary waits
- **WBI signature** — automatic anti-scraping bypass
- **Session rotation** — auto-recovery from 412/352 rate-limit errors
- **Resume support** — skips already downloaded files

## Install

```bash
git clone https://github.com/NeoXue-ai/bili-audio-dl.git
cd bili-audio-dl
```

No `pip install` needed.

## Usage

```bash
# Download all audio from a user's space
python bili_audio_dl.py https://space.bilibili.com/2081722/video

# Using numeric mid
python bili_audio_dl.py 2081722

# Custom output directory + more workers
python bili_audio_dl.py 2081722 -o ./my_audio --workers 8

# Only fetch video list (no download)
python bili_audio_dl.py 2081722 --list-only

# Download from a pre-existing BV list file
python bili_audio_dl.py 2081722 --from-file bvids.txt
```

## How It Works

**Phase 1 — Resolve** (API-bound, rate-limited):
- For each video: call `get_video_info` + `get_audio_url` to get the CDN download link
- Token-bucket rate limiter caps API calls at ~2/sec with burst of 3
- Auto-retry with session rotation on 412/352 errors

**Phase 2 — Download** (CDN-bound, parallel):
- Download audio files from Bilibili CDN using a thread pool
- CDN endpoints are not rate-limited like the API, so parallel workers help significantly
- Default 4 workers, configurable via `--workers`

## Performance

| Mode | 360 videos (~7GB) |
|------|-------------------|
| v1 (sequential, fixed delay) | ~3.5 hours |
| v2 (concurrent, 4 workers) | ~40 min |
| v2 (concurrent, 8 workers) | ~25 min |

Actual speed depends on your network and Bilibili's rate limiting.

## Output Structure

```
bilibili_audio/
├── Video Title 1.m4a
├── Video Title 2.m4a
├── ...
├── bvids.txt          # All BV IDs
└── failed.txt         # Failed downloads (if any)
```

## Requirements

- Python 3.10+
- No third-party packages

## License

MIT
