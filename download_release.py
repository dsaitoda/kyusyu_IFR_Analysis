# -*- coding: utf-8 -*-
"""Download split daily readsb history tar files from ADSB.lol GitHub releases.

Requires the 'curl' command (preinstalled on GitHub Actions and current Windows).
Use --dry-run to list URLs without downloading.
"""
from __future__ import annotations
import argparse
import hashlib
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

JST=timezone(timedelta(hours=9))
BASE="https://github.com/adsblol/globe_history_2026/releases/download"
KNOWN_HASHES={
    "v2026.10.16-planes-readsb-prod-0.tar.aa": "d33bbede5d2fea9abd9c96382e5204d46c2cf7c5baf542f2818850676c8c5d17",
    "v2026.10.16-planes-readsb-prod-0.tar.ab": "2ec7481a2eb8d2e1a03053aa41ddc10bf82c1e7eac572cf84708ee9353567cf9",
    "v2026.10.17-planes-readsb-prod-0.tar.aa": "770dcee6d3ae8774b8cd6bd01bf77044d5db16913f80b6025aef623d19c89550",
    "v2026.10.17-planes-readsb-prod-0.tar.ab": "494d8f32853dc659ffe2381e3eaeb5879792e545ea8acec8698d39901dbdc8de",
}


def sha256(path):
    checksum=hashlib.sha256()
    with open(path,"rb") as f:
        for chunk in iter(lambda:f.read(8*1024*1024),b""):
            checksum.update(chunk)
    return checksum.hexdigest()


def download(target_day, output_dir, dry_run):
    start=datetime.strptime(target_day, "%Y-%m-%d").replace(tzinfo=JST)
    start_utc=start.astimezone(timezone.utc).date()
    end_utc=(start+timedelta(days=1)-timedelta(seconds=1)).astimezone(timezone.utc).date()
    days=sorted({start_utc,end_utc})
    output_dir.mkdir(parents=True,exist_ok=True)
    for day in days:
        tag=f"v{day.strftime('%Y.%m.%d')}-planes-readsb-prod-0"
        for suffix in ["aa","ab"]:
            filename=f"{tag}.tar.{suffix}"
            filepath=output_dir/filename
            url=f"{BASE}/{tag}/{filename}"
            print(url,flush=True)
            if dry_run:
                continue
            expected=KNOWN_HASHES.get(filename)
            if filepath.exists():
                if expected and sha256(filepath).lower()==expected:
                    print("  existing file verified",flush=True)
                    continue
                if not expected and filepath.stat().st_size>50_000_000:
                    print("  existing file (no online checksum available in this script)",flush=True)
                    continue
                print("  re-downloading invalid/incomplete file",flush=True)
                filepath.unlink()
            # Curl retries transient GitHub CDN failures. Github redirects to binary assets.
            cmd=["curl","--fail","--location","--retry","4","--retry-delay","5", "--connect-timeout","30", "--output",str(filepath), url]
            subprocess.run(cmd,check=True)
            if expected:
                got=sha256(filepath)
                if got.lower()!=expected:
                    filepath.unlink(missing_ok=True)
                    raise RuntimeError(f"SHA-256 mismatch: {filename} ({got})")
                print("  SHA-256 verified",flush=True)
            else:
                print("  No built-in SHA256 for this date: verify it against GitHub release page before using.")


if __name__=="__main__":
    parser=argparse.ArgumentParser()
    parser.add_argument("--date",default="2026-10-17",help="Target date in Japan Standard Time, YYYY-MM-DD")
    parser.add_argument("--directory",default="data",help="Directory to store files")
    parser.add_argument("--dry-run",action="store_true")
    args=parser.parse_args()
    download(args.date,Path(args.directory),args.dry_run)
