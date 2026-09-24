#!/usr/bin/env python3
"""
catalog/check.py - checks every radio of catalog/radios.json (standard library only).

For each stream: HEAD (falls back to a GET of the first bytes when HEAD is refused,
which many Icecast servers do), expects HTTP 200 and Content-Type audio/mpeg.
Also flags what the SoundTouch firmware (2022) may not handle: redirects, HTTPS,
HLS playlists. Logos are checked too (HTTP 200, image/*).

    python catalog/check.py              check everything, exit code 1 if a stream is dead
    python catalog/check.py --no-logos
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

UA = "bose-preset-bridge-catalog-check/1.0"


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


OPENER = urllib.request.build_opener(NoRedirect)


def probe(url, method, timeout=8):
    """(status, content_type, location) without following redirects."""
    headers = {"User-Agent": UA}
    if method == "GET":
        headers["Range"] = "bytes=0-2047"
    req = urllib.request.Request(url, method=method, headers=headers)
    try:
        with OPENER.open(req, timeout=timeout) as r:
            if method == "GET":
                r.read(2048)
            return r.status, r.headers.get("Content-Type", ""), ""
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get("Content-Type", ""), e.headers.get("Location", "")
    except Exception as e:
        return None, str(e), ""


def check_stream(url):
    problems = []
    if url.startswith("https://"):
        problems.append("HTTPS (firmware 2022: prefer http)")
    if ".m3u8" in url or "/hls/" in url:
        problems.append("HLS not supported")
    status, ctype, location = probe(url, "HEAD")
    how = "HEAD"
    if status not in (200, 206) or not ctype.startswith("audio/"):
        status, ctype, location = probe(url, "GET")
        how = "GET"
    if status in (301, 302, 303, 307, 308):
        return False, f"redirect ({status}) to {location}", problems
    if status not in (200, 206):
        return False, f"{how} -> {status} {ctype}", problems
    if not ctype.startswith("audio/mpeg"):
        return False, f"{how} -> {status} but Content-Type {ctype!r}", problems
    return True, f"{how} -> {status} {ctype}", problems


def check_logo(url):
    if not url:
        return True, "(no logo)"
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            ctype = r.headers.get("Content-Type", "")
            return ctype.startswith("image/"), f"{r.status} {ctype}"
    except Exception as e:
        return False, str(e)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--catalog", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "radios.json"))
    ap.add_argument("--no-logos", action="store_true")
    args = ap.parse_args()

    with open(args.catalog, encoding="utf-8") as f:
        radios = json.load(f)["radios"]

    ids = [r["id"] for r in radios]
    dupes = {i for i in ids if ids.count(i) > 1}
    if dupes:
        print(f"!! duplicate ids: {sorted(dupes)}")

    def run(r):
        ok, msg, problems = check_stream(r["stream_url"])
        logo = (True, "") if args.no_logos else check_logo(r.get("logo_url", ""))
        return r, ok, msg, problems, logo

    dead = 0
    with ThreadPoolExecutor(8) as pool:
        for r, ok, msg, problems, (logo_ok, logo_msg) in pool.map(run, radios):
            flag = "OK  " if ok else "DEAD"
            dead += not ok
            line = f"{flag} {r['id']:<40} {msg}"
            if problems:
                line += "  [" + "; ".join(problems) + "]"
            if not logo_ok:
                line += f"  [logo: {logo_msg}]"
            print(line)
    print(f"\n{len(radios)} radios, {dead} dead stream(s)")
    return 1 if dead else 0


if __name__ == "__main__":
    sys.exit(main())
