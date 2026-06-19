#!/usr/bin/env python3
"""
bili-audio-dl: Batch download audio from Bilibili user space.

Usage:
    python bili_audio_dl.py <space_url_or_mid> [options]

Examples:
    python bili_audio_dl.py https://space.bilibili.com/2081722/video
    python bili_audio_dl.py 2081722 -o ./audio
    python bili_audio_dl.py 2081722 --format mp3
"""

import argparse
import hashlib
import functools
import json
import os
import random
import re
import sys
import time
import urllib.parse
import urllib.request
import http.cookiejar

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
    """Extract user mid from a space URL or plain number."""
    m = re.search(r"space\.bilibili\.com/(\d+)", input_str)
    if m:
        return int(m.group(1))
    if input_str.strip().isdigit():
        return int(input_str.strip())
    raise ValueError(f"Cannot extract user mid from: {input_str}")


class BiliClient:
    """Minimal Bilibili API client with WBI signature support."""

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
        signed = enc_wbi(params, self.img_key, self.sub_key)
        url = f"https://api.bilibili.com{path}?{urllib.parse.urlencode(signed)}"
        resp = self.opener.open(self._req(url, referer="https://space.bilibili.com"))
        return json.loads(resp.read())

    def get_user_video_count(self, mid: int) -> int:
        data = self._api("/x/space/wbi/arc/search", {
            "mid": mid, "ps": 1, "pn": 1, "order": "pubdate",
            "keyword": "", "tid": 0, "platform": "web", "web_location": "1550101",
        })
        if data["code"] != 0:
            raise RuntimeError(f"API error {data['code']}: {data['message']}")
        return data["data"]["page"]["count"]

    def get_video_list_page(self, mid: int, pn: int, ps: int = 30) -> list[dict]:
        data = self._api("/x/space/wbi/arc/search", {
            "mid": mid, "ps": ps, "pn": pn, "order": "pubdate",
            "keyword": "", "tid": 0, "platform": "web", "web_location": "1550101",
        })
        if data["code"] != 0:
            return []
        return data["data"]["list"]["vlist"]

    def get_video_info(self, bvid: str) -> dict | None:
        url = f"https://api.bilibili.com/x/web-interface/view?bvid={bvid}"
        resp = self.opener.open(self._req(url))
        data = json.loads(resp.read())
        return data["data"] if data["code"] == 0 else None

    def get_audio_url(self, bvid: str, cid: int) -> tuple[str | None, int]:
        data = self._api("/x/player/wbi/playurl", {
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
        req = self._req(url, referer="https://www.bilibili.com")
        resp = self.opener.open(req)
        size = 0
        with open(filepath, "wb") as f:
            while True:
                chunk = resp.read(8192)
                if not chunk:
                    break
                f.write(chunk)
                size += len(chunk)
        return size


def fetch_all_bvids(client: BiliClient, mid: int, delay: float = 2.0) -> list[dict]:
    """Fetch all video BV IDs for a user, with retry and rate-limit handling."""
    total = client.get_user_video_count(mid)
    print(f"User {mid} has {total} videos")

    pages = (total + 29) // 30
    results = {}  # bvid -> title (dedup)
    seen_bvids = set()

    for pn in range(1, pages + 1):
        for attempt in range(5):
            try:
                vlist = client.get_video_list_page(mid, pn)
                if not vlist:
                    break
                for v in vlist:
                    if v["bvid"] not in seen_bvids:
                        seen_bvids.add(v["bvid"])
                        results[v["bvid"]] = v["title"]
                print(f"  Page {pn}/{pages}: {len(vlist)} videos (total: {len(results)})")
                time.sleep(delay + random.uniform(0, 1))
                break
            except Exception as e:
                wait = (attempt + 1) * 5 + random.uniform(0, 3)
                print(f"  Page {pn} attempt {attempt + 1} failed: {e}, retry in {wait:.0f}s")
                time.sleep(wait)
                # Re-init session on persistent failures
                if attempt >= 2:
                    try:
                        client._init_session()
                    except Exception:
                        pass

    return [{"bvid": bvid, "title": title} for bvid, title in results.items()]


def download_audio_batch(
    client: BiliClient,
    videos: list[dict],
    output_dir: str,
    skip_existing: bool = True,
    delay: float = 1.5,
) -> tuple[int, int, list[str]]:
    """Download audio for a list of videos. Returns (success, failed, failed_bvids)."""
    os.makedirs(output_dir, exist_ok=True)
    success = 0
    failed = 0
    failed_bvids = []

    for i, video in enumerate(videos, 1):
        bvid = video["bvid"]
        title = video.get("title", bvid)
        filename = sanitize_filename(title) + ".m4a"
        filepath = os.path.join(output_dir, filename)

        if skip_existing and os.path.exists(filepath):
            print(f"  [{i}/{len(videos)}] Skip (exists): {title}")
            success += 1
            continue

        try:
            info = client.get_video_info(bvid)
            if not info:
                print(f"  [{i}/{len(videos)}] Failed to get info: {bvid}")
                failed += 1
                failed_bvids.append(bvid)
                time.sleep(1)
                continue

            if not video.get("title"):
                title = info["title"]
                filename = sanitize_filename(title) + ".m4a"
                filepath = os.path.join(output_dir, filename)

            audio_url, _ = client.get_audio_url(bvid, info["cid"])
            if not audio_url:
                print(f"  [{i}/{len(videos)}] No audio stream: {title}")
                failed += 1
                failed_bvids.append(bvid)
                time.sleep(1)
                continue

            size = client.download_file(audio_url, filepath)
            size_mb = size / 1024 / 1024
            print(f"  [{i}/{len(videos)}] {title} ({size_mb:.1f}MB)")
            success += 1
            time.sleep(delay)

        except Exception as e:
            print(f"  [{i}/{len(videos)}] Error: {bvid} - {e}")
            failed += 1
            failed_bvids.append(bvid)
            time.sleep(3)

    return success, failed, failed_bvids


def main():
    parser = argparse.ArgumentParser(
        description="Batch download audio from Bilibili user space.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Examples:\n"
               "  python bili_audio_dl.py https://space.bilibili.com/2081722/video\n"
               "  python bili_audio_dl.py 2081722 -o ./audio --delay 2\n",
    )
    parser.add_argument("user", help="Bilibili space URL or user mid (numeric ID)")
    parser.add_argument("-o", "--output", default="./bilibili_audio", help="Output directory (default: ./bilibili_audio)")
    parser.add_argument("--delay", type=float, default=1.5, help="Delay between downloads in seconds (default: 1.5)")
    parser.add_argument("--list-only", action="store_true", help="Only fetch video list, don't download")
    parser.add_argument("--from-file", metavar="FILE", help="Read BV IDs from file instead of fetching")
    args = parser.parse_args()

    mid = extract_mid(args.user)
    output_dir = os.path.abspath(args.output)

    print(f"=== bili-audio-dl ===")
    print(f"User: {mid}")
    print(f"Output: {output_dir}")
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
        videos = fetch_all_bvids(client, mid, delay=args.delay)
        print(f"\nFound {len(videos)} videos")

        # Save BV list
        list_file = os.path.join(output_dir, "bvids.txt")
        os.makedirs(output_dir, exist_ok=True)
        with open(list_file, "w") as f:
            for v in videos:
                f.write(v["bvid"] + "\n")
        print(f"Saved BV list to {list_file}")

    if args.list_only:
        print("\n--list-only mode, skipping download")
        return

    # Step 2: Download audio
    print(f"\nDownloading audio to {output_dir}...")
    success, failed, failed_bvids = download_audio_batch(
        client, videos, output_dir, delay=args.delay
    )

    # Summary
    print(f"\n=== Done ===")
    print(f"Success: {success}")
    print(f"Failed:  {failed}")

    if failed_bvids:
        failed_file = os.path.join(output_dir, "failed.txt")
        with open(failed_file, "w") as f:
            for bvid in failed_bvids:
                f.write(bvid + "\n")
        print(f"Failed BV IDs saved to {failed_file}")


if __name__ == "__main__":
    main()
