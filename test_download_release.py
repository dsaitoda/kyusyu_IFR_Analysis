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
    # The workflow's temporary token must be sent on the metadata API call.
    # Do not test with a live network call; assert the outgoing request header.
    import io
    class StubResponse:
        status = 200
        headers = {"X-RateLimit-Remaining": "980"}
        def __enter__(self):
            return self
        def __exit__(self, *_args):
            return False
        def read(self, *_args):
            return b'{"assets": []}'
    with patch.dict("os.environ", {"GITHUB_TOKEN": "synthetic-token"}, clear=False):
        with patch.object(downloader, "urlopen", return_value=StubResponse()) as mocked:
            response = downloader.get_release(tag)
            assert response == {"assets": []}
            request = mocked.call_args.args[0]
            assert request.get_header("Authorization") == "Bearer synthetic-token"
            assert f"globe_history_2026/releases/tags/{tag}" in request.full_url
    # A transient API 403 should retry rather than fail the whole monthly run.
    from urllib.error import HTTPError
    with patch.dict("os.environ", {"GITHUB_TOKEN": "synthetic-token"}, clear=False):
        denied = HTTPError(
            "https://api.github.com/", 403, "rate limit exceeded",
            {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1"}, None)
        with patch.object(downloader, "urlopen", side_effect=[denied, StubResponse()]) as mocked:
            with patch.object(downloader.time, "sleep", return_value=None):
                assert downloader.get_release(tag) == {"assets": []}
                assert mocked.call_count == 2
    print("PASS: 403 primary rate limit automatically retries")
    print("PASS: authenticated metadata request using the built-in GitHub token")
    print("PASS: 3-part release assets, missing-middle detection, missing-last-part manifest rejection")


if __name__ == "__main__":
    run_tests()
