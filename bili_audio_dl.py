#!/usr/bin/env python3
"""
bili-audio-dl: Batch download audio from Bilibili user space.

Features:
  - Concurrent CDN downloads with thread pool
  - Token-bucket rate limiter for API calls
  - Checkpoint/resume: survives blocks, restarts from where it stopped
  - Cookie auth: --cookie SESSDATA for higher API quotas
  - Exponential backoff with jitter on rate-limit errors
  - Proxy support: --proxy socks5://host:port

Usage:
    python bili_audio_dl.py <space_url_or_mid> [options]
"""

import argparse
import hashlib
import functools
import json
import os
import random
import re
import sys
import threading
import time
import urllib.parse
import urllib.request
import http.cookiejar
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

# fmt: off
MIXIN_KEY_ENC_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
    33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40,
    61, 26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11,
    36, 20, 34, 44, 52,
]
# fmt: on


def get_mixin_key(orig: str) -> str:
    return functools.reduce(lambda s, i: s + orig[i], MIXIN_KEY_ENC_TAB, "")[:32]


def enc_wbi(params: dict, img_key: str, sub_key: str) -> dict:
    mixin_key = get_mixin_key(img_key + sub_key)
    params["wts"] = round(time.time())
    params = dict(sorted(params.items()))
    params = {
        k: "".join(filter(lambda ch: ch not in "!'()*", str(v)))
        for k, v in params.items()
    }
    query = urllib.parse.urlencode(params)
    wbi_sign = hashlib.md5((query + mixin_key).encode()).hexdigest()
    params["w_rid"] = wbi_sign
    return params


def _dm_fingerprint() -> dict:
    """Generate DM fingerprint args to bypass Bilibili anti-scraping.

    Uses fixed base64-encoded strings that mimic real browser WebGL renderer info,
    matching the approach used by downkyicore.
    """
    return {
        "dm_img_str": "V2ViR0wgMS4wIChPcGVuR0wp",
        "dm_cover_img_str": "QU5HTEUgKE5WSURJQSwgTlZJRElBIEdlRm9yY2UgR1RYIDk4MCBEaXJlY3QzRDExIHZzXzVfMCBwc181XzApLCBvciBzaW1pbGFy",
        "dm_img_inter": '{"ds":[],"wh":[0,0,0],"of":[0,0,0]}',
        "dm_img_list": "[]",
    }


def sanitize_filename(name: str, max_len: int = 200) -> str:
    name = re.sub(r'[<>:"/\\|?*]', "_", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name[:max_len]


def extract_mid(input_str: str) -> int:
    m = re.search(r"space\.bilibili\.com/(\d+)", input_str)
    if m:
        return int(m.group(1))
    if input_str.strip().isdigit():
        return int(input_str.strip())
    raise ValueError(f"Cannot extract user mid from: {input_str}")


# ---------------------------------------------------------------------------
# Checkpoint: save/load progress to survive blocks and restarts
# ---------------------------------------------------------------------------

class Checkpoint:
    """Persistent checkpoint for resume support."""

    def __init__(self, path: str):
        self.path = path
        self.lock = threading.Lock()
        self.data = {"resolved": {}, "downloaded": [], "failed": []}
        self._load()

    def _load(self):
        if os.path.exists(self.path):
            try:
                with open(self.path) as f:
                    self.data = json.load(f)
            except (json.JSONDecodeError, IOError):
                pass

    def _save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.data, f, ensure_ascii=False)
        os.replace(tmp, self.path)

    def is_resolved(self, bvid: str) -> dict | None:
        with self.lock:
            return self.data["resolved"].get(bvid)

    def set_resolved(self, bvid: str, info: dict):
        with self.lock:
            self.data["resolved"][bvid] = info
            self._save()

    def is_downloaded(self, bvid: str) -> bool:
        with self.lock:
            return bvid in self.data["downloaded"]

    def mark_downloaded(self, bvid: str):
        with self.lock:
            if bvid not in self.data["downloaded"]:
                self.data["downloaded"].append(bvid)
            # Remove from failed if it was there
            if bvid in self.data["failed"]:
                self.data["failed"].remove(bvid)
            self._save()

    def mark_failed(self, bvid: str):
        with self.lock:
            if bvid not in self.data["failed"]:
                self.data["failed"].append(bvid)
            self._save()

    @property
    def downloaded_set(self) -> set:
        with self.lock:
            return set(self.data["downloaded"])

    @property
    def failed_set(self) -> set:
        with self.lock:
            return set(self.data["failed"])


# ---------------------------------------------------------------------------
# Rate limiter
# ---------------------------------------------------------------------------

class RateLimiter:
    """Token-bucket rate limiter with adaptive cooldown."""

    def __init__(self, rate: float = 2.0, burst: int = 3):
        self.rate = rate
        self.burst = burst
        self.tokens = float(burst)
        self.last = time.monotonic()
        self.lock = threading.Lock()
        self.cooldown_until = 0.0

    def acquire(self):
        while True:
            with self.lock:
                now = time.monotonic()
                # If in cooldown, wait
                if now < self.cooldown_until:
                    wait = self.cooldown_until - now
                    time.sleep(wait)
                    continue
                self.tokens = min(self.burst, self.tokens + (now - self.last) * self.rate)
                self.last = now
                if self.tokens >= 1:
                    self.tokens -= 1
                    return
            time.sleep(0.1)

    def set_cooldown(self, seconds: float):
        """Pause all requests for `seconds` (used when rate-limited by server)."""
        with self.lock:
            self.cooldown_until = time.monotonic() + seconds
            self.tokens = 0


# ---------------------------------------------------------------------------
# Bilibili API client
# ---------------------------------------------------------------------------

class BiliClient:
    """Bilibili API client with WBI signature, cookie auth, and proxy support."""

    UA = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    )

    def __init__(self, cookie: str = "", proxy: str = ""):
        self.cookies: dict[str, str] = {}
        self.img_key = ""
        self.sub_key = ""
        self.lock = threading.Lock()
        self.rate_limiter = RateLimiter(rate=2.0, burst=3)
        self._block_count = 0  # consecutive blocks

        # Parse user cookie (SESSDATA or full cookie string)
        if cookie:
            self._parse_cookie(cookie)

        # Build opener with optional proxy
        handlers = [urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())]
        if proxy:
            proxy_handler = urllib.request.ProxyHandler({
                "http": proxy,
                "https": proxy,
            })
            handlers.append(proxy_handler)
        self.opener = urllib.request.build_opener(*handlers)

        self._init_session()

    def _parse_cookie(self, cookie_str: str):
        """Parse cookie string. Accepts 'SESSDATA=xxx' or 'k1=v1; k2=v2; ...'."""
        for part in cookie_str.split(";"):
            part = part.strip()
            if "=" in part:
                k, v = part.split("=", 1)
                self.cookies[k.strip()] = v.strip()

    def _req(self, url: str, referer: str = "https://www.bilibili.com") -> urllib.request.Request:
        req = urllib.request.Request(url)
        req.add_header("User-Agent", self.UA)
        req.add_header("Referer", referer)
        if self.cookies:
            req.add_header("Cookie", "; ".join(f"{k}={v}" for k, v in self.cookies.items()))
        return req

    def _init_session(self):
        resp = self.opener.open(self._req("https://api.bilibili.com/x/frontend/finger/spi"))
        spi = json.loads(resp.read())
        if spi["code"] == 0:
            self.cookies["buvid3"] = spi["data"]["b_3"]
            self.cookies["buvid4"] = spi["data"]["b_4"]

        resp = self.opener.open(self._req("https://api.bilibili.com/x/web-interface/nav"))
        nav = json.loads(resp.read())
        self.img_key = nav["data"]["wbi_img"]["img_url"].rsplit("/", 1)[1].split(".")[0]
        self.sub_key = nav["data"]["wbi_img"]["sub_url"].rsplit("/", 1)[1].split(".")[0]

        # Check if logged in
        if nav["data"].get("isLogin"):
            uname = nav["data"].get("uname", "?")
            level = nav["data"].get("level_info", {}).get("current_level", "?")
            print(f"  Logged in as: {uname} (Lv.{level})")
        else:
            print("  Anonymous session (consider --cookie for higher limits)")

    def _api(self, path: str, params: dict) -> dict:
        self.rate_limiter.acquire()
        with self.lock:
            signed = enc_wbi(params, self.img_key, self.sub_key)
            url = f"https://api.bilibili.com{path}?{urllib.parse.urlencode(signed)}"
            resp = self.opener.open(self._req(url, referer="https://space.bilibili.com"))
            return json.loads(resp.read())

    def _api_with_retry(self, path: str, params: dict, retries: int = 5) -> dict:
        """API call with exponential backoff + jitter on rate-limit."""
        for attempt in range(retries):
            try:
                data = self._api(path, params)
                code = data.get("code", 0)

                if code == 0:
                    self._block_count = 0
                    return data

                if code in (-352, -799):
                    # Rate-limited: exponential backoff with jitter
                    self._block_count += 1
                    base_wait = min(60, (2 ** self._block_count) * 2)
                    jitter = random.uniform(0, base_wait * 0.5)
                    wait = base_wait + jitter
                    print(f"\n  Rate limited (code {code}), backing off {wait:.0f}s (block #{self._block_count})")
                    self.rate_limiter.set_cooldown(wait)
                    # Reinit session after multiple blocks
                    if self._block_count >= 3:
                        print("  Reinitializing session...")
                        with self.lock:
                            try:
                                self._init_session()
                            except Exception:
                                pass
                        self._block_count = 0
                    continue

                # Other errors (video deleted, etc.) — don't retry
                return data

            except urllib.error.HTTPError as e:
                if e.code == 412:
                    self._block_count += 1
                    wait = min(120, (2 ** self._block_count) * 5) + random.uniform(0, 5)
                    print(f"\n  HTTP 412 blocked, waiting {wait:.0f}s")
                    self.rate_limiter.set_cooldown(wait)
                    if self._block_count >= 2:
                        with self.lock:
                            try:
                                self._init_session()
                            except Exception:
                                pass
                    continue
                if attempt == retries - 1:
                    raise
                time.sleep((attempt + 1) * 3)
            except Exception as e:
                if attempt == retries - 1:
                    raise
                time.sleep((attempt + 1) * 2)

        return {"code": -1, "message": "max retries exceeded"}

    def get_user_video_count(self, mid: int) -> int:
        params = {
            "mid": mid, "ps": 1, "pn": 1, "order": "pubdate",
            "keyword": "", "tid": 0, "platform": "web", "web_location": "1550101",
        }
        params.update(_dm_fingerprint())
        data = self._api_with_retry("/x/space/wbi/arc/search", params)
        if data["code"] != 0:
            raise RuntimeError(f"API error {data['code']}: {data.get('message', '')}")
        return data["data"]["page"]["count"]

    def get_video_list_page(self, mid: int, pn: int, ps: int = 30) -> list[dict]:
        params = {
            "mid": mid, "ps": ps, "pn": pn, "order": "pubdate",
            "keyword": "", "tid": 0, "platform": "web", "web_location": "1550101",
        }
        params.update(_dm_fingerprint())
        data = self._api_with_retry("/x/space/wbi/arc/search", params)
        if data["code"] != 0:
            return []
        return data["data"]["list"]["vlist"]

    def get_video_info(self, bvid: str) -> dict | None:
        data = self._api_with_retry("/x/web-interface/view", {"bvid": bvid})
        return data.get("data") if data["code"] == 0 else None

    def get_audio_url(self, bvid: str, cid: int) -> tuple[str | None, int]:
        data = self._api_with_retry("/x/player/wbi/playurl", {
            "bvid": bvid, "cid": cid, "fnval": 16, "fourk": 1,
        })
        if data["code"] != 0:
            return None, 0
        dash = data["data"].get("dash")
        if dash and dash.get("audio"):
            best = sorted(dash["audio"], key=lambda x: x.get("bandwidth", 0), reverse=True)[0]
            return best.get("baseUrl") or best.get("base_url"), best.get("bandwidth", 0)
        return None, 0

    def get_audio_url_from_webpage(self, bvid: str) -> tuple[str | None, int]:
        """Fallback: extract audio URL from video page's __playinfo__ JSON."""
        url = f"https://www.bilibili.com/video/{bvid}/"
        try:
            resp = self.opener.open(self._req(url), timeout=30)
            html = resp.read().decode("utf-8", errors="replace")
            m = re.search(r"<script>window\.__playinfo__=(.*?)</script>", html)
            if not m:
                return None, 0
            playinfo = json.loads(m.group(1))
            dash = playinfo.get("data", {}).get("dash")
            if dash and dash.get("audio"):
                best = sorted(dash["audio"], key=lambda x: x.get("bandwidth", 0), reverse=True)[0]
                return best.get("baseUrl") or best.get("base_url"), best.get("bandwidth", 0)
        except Exception:
            pass
        return None, 0

    def download_file(self, url: str, filepath: str) -> int:
        req = self._req(url, referer="https://www.bilibili.com")
        resp = self.opener.open(req, timeout=120)
        size = 0
        with open(filepath, "wb") as f:
            while True:
                chunk = resp.read(65536)
                if not chunk:
                    break
                f.write(chunk)
                size += len(chunk)
        return size


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------

def fetch_all_bvids(client: BiliClient, mid: int, ckpt: Checkpoint) -> list[dict]:
    total = client.get_user_video_count(mid)
    print(f"User {mid} has {total} videos")

    pages = (total + 29) // 30
    results = {}
    seen = set()

    for pn in range(1, pages + 1):
        for attempt in range(5):
            try:
                vlist = client.get_video_list_page(mid, pn)
                if not vlist:
                    break
                for v in vlist:
                    if v["bvid"] not in seen:
                        seen.add(v["bvid"])
                        results[v["bvid"]] = v["title"]
                sys.stdout.write(f"\r  Fetched page {pn}/{pages} ({len(results)} videos)")
                sys.stdout.flush()
                break
            except Exception as e:
                if attempt == 4:
                    print(f"\n  Page {pn} failed: {e}")
                else:
                    time.sleep((attempt + 1) * 3)

    print()
    return [{"bvid": bvid, "title": title} for bvid, title in results.items()]


def _resolve_one(client: BiliClient, video: dict, output_dir: str, ckpt: Checkpoint) -> dict:
    """Resolve video info + audio URL. Uses checkpoint cache."""
    bvid = video["bvid"]
    title = video.get("title", "")
    result = {"bvid": bvid, "title": title, "status": "pending"}

    # Already downloaded?
    if ckpt.is_downloaded(bvid):
        result["status"] = "skip"
        return result

    # Already resolved? (cached from previous run)
    cached = ckpt.is_resolved(bvid)
    if cached and cached.get("audio_url"):
        # Verify file still exists
        filepath = cached.get("filepath", "")
        if filepath and os.path.exists(filepath):
            result["status"] = "skip"
            ckpt.mark_downloaded(bvid)
            return result
        result.update(cached)
        result["status"] = "ready"
        return result

    try:
        info = client.get_video_info(bvid)
        if not info:
            result["status"] = "no_info"
            return result

        result["title"] = info["title"]
        result["cid"] = info["cid"]
        filepath = os.path.join(output_dir, sanitize_filename(info["title"]) + ".m4a")
        result["filepath"] = filepath

        if os.path.exists(filepath):
            result["status"] = "skip"
            ckpt.mark_downloaded(bvid)
            return result

        audio_url, bw = client.get_audio_url(bvid, info["cid"])
        if not audio_url:
            # Fallback: try extracting from video page HTML
            audio_url, bw = client.get_audio_url_from_webpage(bvid)
        if not audio_url:
            result["status"] = "no_audio"
            return result

        result["audio_url"] = audio_url
        result["bandwidth"] = bw
        result["status"] = "ready"

        # Cache the resolved info
        ckpt.set_resolved(bvid, {
            "title": info["title"],
            "cid": info["cid"],
            "audio_url": audio_url,
            "bandwidth": bw,
            "filepath": filepath,
        })
        return result

    except Exception as e:
        result["status"] = "error"
        result["error"] = str(e)
        return result


def _download_one(client: BiliClient, task: dict, ckpt: Checkpoint) -> dict:
    """Download a single audio file."""
    bvid = task["bvid"]
    try:
        size = client.download_file(task["audio_url"], task["filepath"])
        task["status"] = "done"
        task["size"] = size
        ckpt.mark_downloaded(bvid)
        return task
    except Exception as e:
        task["status"] = "download_error"
        task["error"] = str(e)
        ckpt.mark_failed(bvid)
        return task


def download_audio_batch(
    client: BiliClient,
    videos: list[dict],
    output_dir: str,
    ckpt: Checkpoint,
    workers: int = 4,
) -> tuple[int, int, list[str]]:
    os.makedirs(output_dir, exist_ok=True)
    total = len(videos)

    # Phase 1: Resolve
    print(f"Phase 1: Resolving {total} videos...")
    resolved = []
    skipped = 0
    failed_resolve = 0

    for i, video in enumerate(videos, 1):
        result = _resolve_one(client, video, output_dir, ckpt)
        if result["status"] == "skip":
            skipped += 1
        elif result["status"] == "ready":
            resolved.append(result)
        else:
            failed_resolve += 1
            if result.get("error"):
                print(f"\n  [{i}/{total}] Resolve failed: {result['bvid']} - {result['error']}")

        if i % 10 == 0 or i == total:
            sys.stdout.write(
                f"\r  Resolved {i}/{total} "
                f"(ready: {len(resolved)}, skip: {skipped}, fail: {failed_resolve})"
            )
            sys.stdout.flush()

    print(f"\n  Ready to download: {len(resolved)} files")

    if not resolved:
        return skipped, failed_resolve, []

    # Phase 2: Download
    print(f"Phase 2: Downloading {len(resolved)} files with {workers} workers...")
    success = 0
    failed = 0
    failed_bvids = []
    done = skipped
    total_bytes = 0
    start_time = time.monotonic()

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_download_one, client, task, ckpt): task for task in resolved}

        for future in as_completed(futures):
            result = future.result()
            done += 1

            if result["status"] == "done":
                success += 1
                size_mb = result["size"] / 1024 / 1024
                total_bytes += result["size"]
                elapsed = time.monotonic() - start_time
                files_speed = (done - skipped) / elapsed if elapsed > 0 else 0
                bw_speed = total_bytes / 1024 / 1024 / elapsed if elapsed > 0 else 0
                eta = (len(resolved) - (done - skipped)) / files_speed if files_speed > 0 else 0
                sys.stdout.write(
                    f"\r  [{done}/{total}] {result['title'][:35]:<35s} ({size_mb:.1f}MB) "
                    f"| {bw_speed:.1f}MB/s | {files_speed:.1f}f/s | ETA {eta:.0f}s"
                )
                sys.stdout.flush()
            else:
                failed += 1
                failed_bvids.append(result["bvid"])
                print(f"\n  [{done}/{total}] Failed: {result['bvid']} - {result.get('error', result['status'])}")

    elapsed = time.monotonic() - start_time
    print(f"\n  Download phase: {elapsed:.0f}s ({success} files, "
          f"{total_bytes / 1024 / 1024:.1f}MB, {total_bytes / 1024 / 1024 / elapsed:.1f}MB/s)")

    return skipped + success, failed + failed_resolve, failed_bvids


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Batch download audio from Bilibili user space.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python bili_audio_dl.py https://space.bilibili.com/2081722/video\n"
            "  python bili_audio_dl.py 2081722 --workers 8\n"
            "  python bili_audio_dl.py 2081722 --cookie 'SESSDATA=abc123'\n"
            "  python bili_audio_dl.py 2081722 --proxy socks5://127.0.0.1:1080\n"
        ),
    )
    parser.add_argument("user", help="Bilibili space URL or user mid")
    parser.add_argument("-o", "--output", default="./bilibili_audio", help="Output directory")
    parser.add_argument("--workers", type=int, default=4, help="Parallel download workers (default: 4)")
    parser.add_argument("--cookie", default="", help="Bilibili cookie (SESSDATA=xxx or full cookie string)")
    parser.add_argument("--proxy", default="", help="Proxy URL (e.g. socks5://127.0.0.1:1080)")
    parser.add_argument("--list-only", action="store_true", help="Only fetch video list, don't download")
    parser.add_argument("--from-file", metavar="FILE", help="Read BV IDs from file")
    args = parser.parse_args()

    mid = extract_mid(args.user)
    output_dir = os.path.abspath(args.output)

    print(f"=== bili-audio-dl ===")
    print(f"User: {mid}")
    print(f"Output: {output_dir}")
    print(f"Workers: {args.workers}")
    if args.proxy:
        print(f"Proxy: {args.proxy}")
    print()

    # Checkpoint for resume support
    ckpt_path = os.path.join(output_dir, ".checkpoint.json")
    os.makedirs(output_dir, exist_ok=True)
    ckpt = Checkpoint(ckpt_path)

    prev_downloaded = len(ckpt.downloaded_set)
    if prev_downloaded > 0:
        print(f"Resuming from checkpoint: {prev_downloaded} already downloaded")

    client = BiliClient(cookie=args.cookie, proxy=args.proxy)

    # Step 1: Get video list
    if args.from_file:
        print(f"Reading BV IDs from {args.from_file}...")
        with open(args.from_file) as f:
            videos = [{"bvid": line.strip(), "title": ""} for line in f if line.strip()]
        print(f"Loaded {len(videos)} BV IDs")
    else:
        print("Fetching video list...")
        videos = fetch_all_bvids(client, mid, ckpt)
        print(f"Found {len(videos)} videos")

        list_file = os.path.join(output_dir, "bvids.txt")
        with open(list_file, "w") as f:
            for v in videos:
                f.write(v["bvid"] + "\n")
        print(f"Saved BV list to {list_file}")

    if args.list_only:
        print("\n--list-only mode, done.")
        return

    # Step 2: Download
    print()
    start = time.monotonic()
    success, failed, failed_bvids = download_audio_batch(
        client, videos, output_dir, ckpt, workers=args.workers
    )
    total_time = time.monotonic() - start

    print(f"\n=== Done ({total_time:.0f}s) ===")
    print(f"Success: {success}")
    print(f"Failed:  {failed}")
    print(f"Checkpoint: {ckpt_path}")

    if failed_bvids:
        failed_file = os.path.join(output_dir, "failed.txt")
        with open(failed_file, "w") as f:
            for bvid in failed_bvids:
                f.write(bvid + "\n")
        print(f"Failed list: {failed_file}")

    if failed > 0:
        print(f"\nTip: Run again to retry failed downloads (checkpoint will skip completed ones)")


if __name__ == "__main__":
    main()
