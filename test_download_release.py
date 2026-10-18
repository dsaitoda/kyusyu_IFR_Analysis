"""Regression tests for GitHub release assets changing from 2 to 3+ parts."""
import hashlib
import json
import tempfile
from pathlib import Path
from unittest.mock import patch
from datetime import datetime, timezone

import download_release as downloader
import kyushu_continuity as analyzer


def fake_release(tag, bodies):
    assets = []
    for suffix, data in bodies.items():
        assets.append({
            "name": f"{tag}.tar.{suffix}",
            "size": len(data),
            "digest": "sha256:" + hashlib.sha256(data).hexdigest(),
            "browser_download_url": "https://example.invalid/" + suffix,
        })
    return {"assets": list(reversed(assets))}


def run_tests():
    tag = "v2026.10.15-planes-readsb-prod-0"
    contents = {"aa": b"file-part-aa", "ab": b"file-part-ab", "ac": b"file-part-ac"}
    good = fake_release(tag, contents)
    found = downloader.list_tar_assets(good, tag)
    assert [x["name"] for x in found] == [f"{tag}.tar.{x}" for x in ("aa", "ab", "ac")]

    missing = {"assets": [x for x in found if x["name"].endswith((".aa", ".ac"))]}
    try:
        downloader.list_tar_assets(missing, tag)
    except RuntimeError:
        pass
    else:
        raise AssertionError("Missing middle part not rejected")

    with tempfile.TemporaryDirectory() as td:
        folder = Path(td)
        for part, body in contents.items():
            (folder / f"{tag}.tar.{part}").write_bytes(body)
        manifest = {"release": tag, "parts": [
            {"name": a["name"], "size": a["size"], "digest": a["digest"]}
            for a in found
        ]}
        (folder / f"{tag}.tar.manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        assert len(analyzer.discover_archive_parts(folder, datetime(2026, 10, 15, tzinfo=timezone.utc))) == 3
        (folder / f"{tag}.tar.ac").unlink()
        try:
            analyzer.discover_archive_parts(folder, datetime(2026, 10, 15, tzinfo=timezone.utc))
        except ValueError:
            pass
        else:
            raise AssertionError("Missing last part not rejected")
    print("PASS: 3-part release assets, missing-middle detection, missing-last-part manifest rejection")


if __name__ == "__main__":
    run_tests()
