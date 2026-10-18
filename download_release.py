# -*- coding: utf-8 -*-
"""Download *all* split ADSB.lol readsb tar parts, verified against GitHub assets.

Do not assume exactly two parts: 2026-10-15 uses aa/ab/ac, for example.
Requires curl (available on GitHub Actions and current Windows).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.request import Request, urlopen

JST = timezone(timedelta(hours=9))
RELEASES_API = "https://api.github.com/repos/adsblol/globe_history_2026/releases/tags/"
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
    request = Request(
        RELEASES_API + tag,
        headers={"Accept": "application/vnd.github+json", "User-Agent": USER_AGENT},
    )
    with urlopen(request, timeout=60) as response:
        return json.load(response)


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
