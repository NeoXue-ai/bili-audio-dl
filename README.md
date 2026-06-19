# bili-audio-dl

Batch download audio from Bilibili (B站) user space. No external dependencies — pure Python 3.10+.

## Features

- Fetch all video BV IDs from any public Bilibili user space
- Download audio streams (m4a) with highest available quality
- Auto retry with rate-limit handling (WBI signature + session rotation)
- Resume support — skips already downloaded files
- Zero dependencies — only uses Python standard library

## Install

```bash
git clone https://github.com/lonnie/bili-audio-dl.git
cd bili-audio-dl
```

No `pip install` needed.

## Usage

```bash
# Download all audio from a user's space
python bili_audio_dl.py https://space.bilibili.com/2081722/video

# Using numeric mid
python bili_audio_dl.py 2081722

# Custom output directory
python bili_audio_dl.py 2081722 -o ./my_audio

# Only fetch video list (no download)
python bili_audio_dl.py 2081722 --list-only

# Download from a pre-existing BV list file
python bili_audio_dl.py 2081722 --from-file bvids.txt

# Adjust delay between requests (seconds)
python bili_audio_dl.py 2081722 --delay 3
```

## How It Works

1. **Session init** — Gets `buvid3`/`buvid4` device fingerprint cookies and WBI signature keys from Bilibili API
2. **Fetch video list** — Calls `/x/space/wbi/arc/search` with WBI-signed requests, rotating sessions on rate-limit (412/352)
3. **Download audio** — For each video, gets the DASH audio stream URL via `/x/player/wbi/playurl` and downloads the m4a file

## Rate Limiting

Bilibili has aggressive anti-scraping. If you hit rate limits:

- Increase `--delay` (e.g. `--delay 3`)
- The tool auto-retries with fresh sessions, but heavy scraping may still get blocked
- Consider running during off-peak hours (late night CN time)

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
