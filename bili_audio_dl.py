#!/usr/bin/env python3
"""
bili-audio-dl: Batch download audio from Bilibili user space.

Usage:
    python bili_audio_dl.py <space_url_or_mid> [options]

Examples:
    python bili_audio_dl.py https://space.bilibili.com/2081722/video
    python bili_audio_dl.py 2081722 -o ./audio
    python bili_audio_dl.py 2081722 --workers 8
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


class RateLimiter:
    """Token-bucket rate limiter for thread-safe API throttling."""

    def __init__(self, rate: float = 2.0, burst: int = 3):
        self.rate = rate  # tokens per second
        self.burst = burst
        self.tokens = float(burst)
        self.last = time.monotonic()
        self.lock = threading.Lock()

    def acquire(self):
        while True:
            with self.lock:
                now = time.monotonic()
                self.tokens = min(self.burst, self.tokens + (now - self.last) * self.rate)
                self.last = now
                if self.tokens >= 1:
                    self.tokens -= 1
                    return
            time.sleep(0.1)


class BiliClient:
    """Bilibili API client with WBI signature and connection pooling."""

    UA = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    )

    def __init__(self):
        self.cj = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.cj)
        )
        self.cookies: dict[str, str] = {}
        self.img_key = ""
        self.sub_key = ""
        self.lock = threading.Lock()
        self.rate_limiter = RateLimiter(rate=2.0, burst=3)
        self._init_session()

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

    def _api(self, path: str, params: dict) -> dict:
        self.rate_limiter.acquire()
        with self.lock:
            signed = enc_wbi(params, self.img_key, self.sub_key)
            url = f"https://api.bilibili.com{path}?{urllib.parse.urlencode(signed)}"
            resp = self.opener.open(self._req(url, referer="https://space.bilibili.com"))
            return json.loads(resp.read())

    def _api_with_retry(self, path: str, params: dict, retries: int = 3) -> dict:
        for attempt in range(retries):
            try:
                data = self._api(path, params)
                if data["code"] == -352 or data["code"] == -799:
                    wait = (attempt + 1) * 3 + random.uniform(0, 2)
                    time.sleep(wait)
                    if attempt >= 1:
                        with self.lock:
                            self._init_session()
                    continue
                return data
            except Exception as e:
                if attempt == retries - 1:
                    raise
                time.sleep((attempt + 1) * 2)
                if attempt >= 1:
                    with self.lock:
                        try:
                            self._init_session()
                        except Exception:
                            pass
        return {"code": -1, "message": "max retries exceeded"}

    def get_user_video_count(self, mid: int) -> int:
        data = self._api_with_retry("/x/space/wbi/arc/search", {
            "mid": mid, "ps": 1, "pn": 1, "order": "pubdate",
            "keyword": "", "tid": 0, "platform": "web", "web_location": "1550101",
        })
        if data["code"] != 0:
            raise RuntimeError(f"API error {data['code']}: {data.get('message', '')}")
        return data["data"]["page"]["count"]

    def get_video_list_page(self, mid: int, pn: int, ps: int = 30) -> list[dict]:
        data = self._api_with_retry("/x/space/wbi/arc/search", {
            "mid": mid, "ps": ps, "pn": pn, "order": "pubdate",
            "keyword": "", "tid": 0, "platform": "web", "web_location": "1550101",
        })
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

    def download_file(self, url: str, filepath: str) -> int:
        # CDN downloads don't need rate limiting — separate from API
        req = self._req(url, referer="https://www.bilibili.com")
        resp = self.opener.open(req, timeout=60)
        size = 0
        with open(filepath, "wb") as f:
            while True:
                chunk = resp.read(65536)
                if not chunk:
                    break
                f.write(chunk)
                size += len(chunk)
        return size


def fetch_all_bvids(client: BiliClient, mid: int) -> list[dict]:
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
                    print(f"\n  Page {pn} failed after 5 attempts: {e}")
                else:
                    time.sleep((attempt + 1) * 3)

    print()
    return [{"bvid": bvid, "title": title} for bvid, title in results.items()]


def _resolve_one(client: BiliClient, video: dict, output_dir: str, skip_existing: bool) -> dict:
    """Resolve video info + audio URL for one video. Returns enriched video dict."""
    bvid = video["bvid"]
    title = video.get("title", "")
    result = {"bvid": bvid, "title": title, "status": "pending"}

    # Check if already downloaded
    if title:
        filepath = os.path.join(output_dir, sanitize_filename(title) + ".m4a")
        if skip_existing and os.path.exists(filepath):
            result["status"] = "skip"
            result["filepath"] = filepath
            return result

    try:
        info = client.get_video_info(bvid)
        if not info:
            result["status"] = "no_info"
            return result

        result["title"] = info["title"]
        result["cid"] = info["cid"]

        filepath = os.path.join(output_dir, sanitize_filename(info["title"]) + ".m4a")
        if skip_existing and os.path.exists(filepath):
            result["status"] = "skip"
            result["filepath"] = filepath
            return result

        audio_url, bw = client.get_audio_url(bvid, info["cid"])
        if not audio_url:
            result["status"] = "no_audio"
            return result

        result["audio_url"] = audio_url
        result["bandwidth"] = bw
        result["filepath"] = filepath
        result["status"] = "ready"
        return result

    except Exception as e:
        result["status"] = "error"
        result["error"] = str(e)
        return result


def _download_one(client: BiliClient, task: dict) -> dict:
    """Download a single audio file. task must have audio_url and filepath."""
    try:
        size = client.download_file(task["audio_url"], task["filepath"])
        task["status"] = "done"
        task["size"] = size
        return task
    except Exception as e:
        task["status"] = "download_error"
        task["error"] = str(e)
        return task


def download_audio_batch(
    client: BiliClient,
    videos: list[dict],
    output_dir: str,
    workers: int = 4,
    skip_existing: bool = True,
) -> tuple[int, int, list[str]]:
    os.makedirs(output_dir, exist_ok=True)
    total = len(videos)

    # Phase 1: Resolve all video info + audio URLs (API-bound, rate-limited)
    print(f"Phase 1: Resolving {total} video info + audio URLs...")
    resolved = []
    skipped = 0
    failed_resolve = 0

    for i, video in enumerate(videos, 1):
        result = _resolve_one(client, video, output_dir, skip_existing)
        if result["status"] == "skip":
            skipped += 1
        elif result["status"] == "ready":
            resolved.append(result)
        else:
            failed_resolve += 1
            if result.get("error"):
                print(f"\n  [{i}/{total}] Resolve failed: {result['bvid']} - {result['error']}")

        if i % 10 == 0 or i == total:
            sys.stdout.write(f"\r  Resolved {i}/{total} (ready: {len(resolved)}, skip: {skipped}, fail: {failed_resolve})")
            sys.stdout.flush()

    print(f"\n  Ready to download: {len(resolved)} files")

    if not resolved:
        return skipped, failed_resolve, []

    # Phase 2: Download audio files in parallel (CDN-bound, not rate-limited)
    print(f"Phase 2: Downloading {len(resolved)} files with {workers} workers...")
    success = 0
    failed = 0
    failed_bvids = []
    done = skipped
    start_time = time.monotonic()

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_download_one, client, task): task for task in resolved}

        for future in as_completed(futures):
            result = future.result()
            done += 1

            if result["status"] == "done":
                success += 1
                size_mb = result["size"] / 1024 / 1024
                elapsed = time.monotonic() - start_time
                speed = (done - skipped) / elapsed if elapsed > 0 else 0
                eta = (len(resolved) - (done - skipped)) / speed if speed > 0 else 0
                sys.stdout.write(
                    f"\r  [{done}/{total}] {result['title'][:40]:<40s} ({size_mb:.1f}MB) "
                    f"| {speed:.1f}/s | ETA {eta:.0f}s"
                )
                sys.stdout.flush()
            else:
                failed += 1
                failed_bvids.append(result["bvid"])
                print(f"\n  [{done}/{total}] Failed: {result['bvid']} - {result.get('error', result['status'])}")

    elapsed = time.monotonic() - start_time
    print(f"\n  Download phase: {elapsed:.0f}s ({success} files, {success/elapsed:.1f} files/s)")

    return skipped + success, failed + failed_resolve, failed_bvids


def main():
    parser = argparse.ArgumentParser(
        description="Batch download audio from Bilibili user space.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python bili_audio_dl.py https://space.bilibili.com/2081722/video\n"
            "  python bili_audio_dl.py 2081722 -o ./audio --workers 8\n"
        ),
    )
    parser.add_argument("user", help="Bilibili space URL or user mid")
    parser.add_argument("-o", "--output", default="./bilibili_audio", help="Output directory")
    parser.add_argument("--workers", type=int, default=4, help="Parallel download workers (default: 4)")
    parser.add_argument("--list-only", action="store_true", help="Only fetch video list, don't download")
    parser.add_argument("--from-file", metavar="FILE", help="Read BV IDs from file instead of fetching")
    args = parser.parse_args()

    mid = extract_mid(args.user)
    output_dir = os.path.abspath(args.output)

    print(f"=== bili-audio-dl ===")
    print(f"User: {mid}")
    print(f"Output: {output_dir}")
    print(f"Workers: {args.workers}")
    print()

    client = BiliClient()

    # Step 1: Get video list
    if args.from_file:
        print(f"Reading BV IDs from {args.from_file}...")
        with open(args.from_file) as f:
            videos = [{"bvid": line.strip(), "title": ""} for line in f if line.strip()]
        print(f"Loaded {len(videos)} BV IDs")
    else:
        print("Fetching video list...")
        videos = fetch_all_bvids(client, mid)
        print(f"Found {len(videos)} videos")

        list_file = os.path.join(output_dir, "bvids.txt")
        os.makedirs(output_dir, exist_ok=True)
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
        client, videos, output_dir, workers=args.workers
    )
    total_time = time.monotonic() - start

    print(f"\n=== Done ({total_time:.0f}s) ===")
    print(f"Success: {success}")
    print(f"Failed:  {failed}")

    if failed_bvids:
        failed_file = os.path.join(output_dir, "failed.txt")
        with open(failed_file, "w") as f:
            for bvid in failed_bvids:
                f.write(bvid + "\n")
        print(f"Failed list: {failed_file}")


if __name__ == "__main__":
    main()
