# -*- coding: utf-8 -*-
"""Download *all* split ADSB.lol readsb tar parts, verified against GitHub assets.

Do not assume exactly two parts: 2026-10-15 uses aa/ab/ac, for example.
Requires curl (available on GitHub Actions and current Windows).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

JST = timezone(timedelta(hours=9))
RELEASES_API_TEMPLATE = "https://api.github.com/repos/adsblol/globe_history_{year}/releases/tags/"
USER_AGENT = "kyushu-adsb-continuity/1.1"


def sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for block in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def list_tar_assets(release, tag):
    """Return ordered, contiguous .aa/.ab/.ac/... assets from release metadata."""
    pattern = re.compile(rf"^{re.escape(tag)}\.tar\.a[a-z]$")
    assets = sorted(
        (a for a in release.get("assets", []) if pattern.fullmatch(a.get("name", ""))),
        key=lambda a: a["name"],
    )
    names = [asset["name"] for asset in assets]
    expected = [f"{tag}.tar.a{chr(ord('a') + i)}" for i in range(len(assets))]
    if not assets or len(assets) > 26 or names != expected:
        raise RuntimeError(f"Release {tag}: incomplete/non-contiguous split tar assets: {names}")
    for a in assets:
        if not a.get("browser_download_url") or not isinstance(a.get("size"), int) or a["size"] <= 0:
            raise RuntimeError(f"Release {tag}: missing URL/size metadata: {a.get('name')}")
        value = a.get("digest")
        if value and not re.fullmatch(r"sha256:[a-fA-F0-9]{64}", value):
            raise RuntimeError(f"Release {tag}: unsupported checksum: {value}")
    return assets


def get_release(tag):
    """Fetch one release using the workflow's GITHUB_TOKEN if supplied.

    Unauthenticated GitHub API access is limited per shared IP, which breaks
    a matrix of 30 daily jobs. Do not log the token or the Authorization header.
    """
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": USER_AGENT,
        "X-GitHub-Api-Version": "2022-11-28",
    }
    token = os.getenv("GITHUB_TOKEN") or os.getenv("GH_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token.strip()}"
    else:
        print("WARNING: no GitHub API token supplied; public API rate limit may be exceeded",
              flush=True)
    url = RELEASES_API_TEMPLATE.format(year=tag[1:5]) + tag
    request = Request(url, headers=headers)
    for attempt in range(5):
        try:
            with urlopen(request, timeout=60) as response:
                print(f"GitHub release API {tag}: HTTP {response.status}, "
                      f"remaining={response.headers.get('X-RateLimit-Remaining', '?')}",
                      flush=True)
                return json.load(response)
        except HTTPError as exc:
            status = exc.code
            remaining = exc.headers.get("X-RateLimit-Remaining", "")
            reset = exc.headers.get("X-RateLimit-Reset")
            retry_after = exc.headers.get("Retry-After")
            # 404 means the day's archive is not published; no retry can fix it.
            if status == 404:
                raise RuntimeError(f"ADSB.lol release not published: {tag} (HTTP 404)") from exc
            retryable = status in (429, 500, 502, 503, 504) or (
                status == 403 and (remaining == "0" or retry_after is not None))
            if not retryable or attempt == 4:
                raise RuntimeError(
                    f"GitHub release API failed for {tag}: HTTP {status}, "
                    f"remaining={remaining or '?'}, "
                    f"authenticated={'yes' if token else 'no'}") from exc
            wait = min(10 * (2 ** attempt), 120)
            if retry_after:
                try:
                    wait = max(wait, int(retry_after))
                except ValueError:
                    pass
            if remaining == "0" and reset:
                try:
                    wait = max(wait, int(reset) - time.time() + 5)
                except ValueError:
                    pass
            print(f"GitHub API HTTP {status}, rate-limited; retry "
                  f"{attempt + 1}/4 after {math.ceil(max(1, wait))} seconds", flush=True)
            time.sleep(max(1, wait))
        except URLError as exc:
            if attempt == 4:
                raise RuntimeError(f"Network problem reading release {tag}") from exc
            wait = 10 * 2 ** attempt
            print(f"Network error contacting GitHub release API; retry in {wait}s", flush=True)
            time.sleep(wait)
    raise RuntimeError(f"Could not fetch release metadata for {tag}")


def check_download(path, asset):
    expected_size = asset["size"]
    actual_size = path.stat().st_size
    if actual_size != expected_size:
        raise RuntimeError(f"File size mismatch: {path.name} ({actual_size:,} vs {expected_size:,} bytes)")
    expected_hash = (asset.get("digest") or "").removeprefix("sha256:")
    if expected_hash and sha256(path).lower() != expected_hash.lower():
        raise RuntimeError(f"SHA-256 mismatch: {path.name}")
    if not expected_hash:
        print(f"  WARNING: {path.name}: API does not publish a checksum (size checked only)", flush=True)


def download(target_day, output_dir, dry_run=False):
    start = datetime.strptime(target_day, "%Y-%m-%d").replace(tzinfo=JST)
    if start > datetime.now(JST):
        raise ValueError("Future date is not available in historical archives")
    last_second = start + timedelta(days=1, seconds=-1)
    days = sorted({start.astimezone(timezone.utc).date(), last_second.astimezone(timezone.utc).date()})
    output_dir.mkdir(parents=True, exist_ok=True)

    for day in days:
        tag = f"v{day.strftime('%Y.%m.%d')}-planes-readsb-prod-0"
        assets = list_tar_assets(get_release(tag), tag)
        print(f"Release {tag}: {len(assets)} parts, {sum(a['size'] for a in assets)/1e9:.2f} GB", flush=True)
        for asset in assets:
            name = asset["name"]
            path = output_dir / name
            url = asset["browser_download_url"]
            print(f"  {name}: {asset['size']:,} bytes; {url}", flush=True)
            if dry_run:
                continue
            if path.exists():
                try:
                    check_download(path, asset)
                    print("    existing file verified", flush=True)
                    continue
                except RuntimeError as exc:
                    print(f"    invalid existing file: {exc}; downloading again", flush=True)
                    path.unlink()
            partial_path = path.with_name(path.name + ".part")
            partial_path.unlink(missing_ok=True)
            cmd = [
                "curl", "--fail", "--location", "--retry", "5", "--retry-delay", "5",
                "--connect-timeout", "30", "--output", str(partial_path), url,
            ]
            try:
                subprocess.run(cmd, check=True)
                check_download(partial_path, asset)
                partial_path.replace(path)
            except Exception:
                partial_path.unlink(missing_ok=True)
                raise
            print("    size and SHA-256 verified" if asset.get("digest") else "    size verified", flush=True)

        if not dry_run:
            # The analyzer checks this manifest to prevent accidental omission of .ac/.ad parts.
            manifest = {
                "release": tag,
                "parts": [
                    {"name": a["name"], "size": a["size"], "digest": a.get("digest")}
                    for a in assets
                ],
            }
            manifest_path = output_dir / f"{tag}.tar.manifest.json"
            temp_manifest = manifest_path.with_suffix(".json.tmp")
            temp_manifest.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
            temp_manifest.replace(manifest_path)
            print(f"  complete and verified: {manifest_path.name}", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default="2026-10-17", help="JST date, YYYY-MM-DD")
    parser.add_argument("--directory", default="data")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    download(args.date, Path(args.directory), args.dry_run)
