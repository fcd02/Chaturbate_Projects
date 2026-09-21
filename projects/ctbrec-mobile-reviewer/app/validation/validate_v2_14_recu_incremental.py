#!/usr/bin/env python3
"""v2.14 Recu incremental memory + browser fetch integration regression checks."""
from __future__ import annotations

import importlib.util
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


m = load("ctbrec_mosaic_v214_test", ROOT / "ctbrec_mosaic_sort_lite.py")
s = load("ctbrec_server_v214_test", ROOT / "ctbrec_mobile_server.py")


def card(vid: int, date: str, slug: str = "cumshot", t: int = 10) -> str:
    return (
        f'<div class="video-thumb" data-id="{vid}">'
        f'<a href="/video/{vid}/play?t={t}&k={slug}">x</a>'
        f'<span class="video-date">{date}</span></div>'
    )


root_html = '<a href="/performer/test/kinks/cumshot">cumshot</a>'
performer_html = card(300, "2026-09-11 12:00:00")
page1_initial = card(200, "2026-09-10 12:00:00") + '<a href="/performer/test/kinks/cumshot/page/2">2</a>'
page2_initial = card(100, "2026-09-09 12:00:00")
page1_incremental = (
    card(300, "2026-09-11 12:00:00")
    + card(200, "2026-09-10 12:00:00")
    + '<a href="/performer/test/kinks/cumshot/page/2">2</a>'
)
phase = {"value": 1}
calls: list[tuple] = []


def fetch_one(url: str, expect_listing: bool = False) -> str:
    calls.append(("one", url, expect_listing))
    if url.endswith("/performer/test"):
        return performer_html
    if url.endswith("/performer/test/kinks"):
        return root_html
    if url.endswith("/performer/test/kinks/cumshot"):
        return page1_initial if phase["value"] == 1 else page1_incremental
    if url.endswith("/page/2"):
        return page2_initial
    raise AssertionError(url)


def fetch_many(urls, expect_listing=True, max_workers=12):
    calls.append(("many", tuple(urls), expect_listing, max_workers))
    return {url: fetch_one(url, expect_listing) for url in urls}


with tempfile.TemporaryDirectory() as td:
    cache = m.RecuMetadataCache(Path(td) / "cache.json")
    settings = dict(m.DEFAULT_SETTINGS)
    settings.update({
        "recu_base_url": "https://recu.me",
        "recu_scrape_comments": False,
        "recu_throttle_seconds": 0,
        "recu_concurrent_requests": 12,
    })
    first = m.scrape_recu_model(
        "test", settings, cache, threading.Event(), force=True,
        fetch_html_fn=fetch_one, fetch_many_fn=fetch_many,
    )
    assert {item.video_id for item in first.moments} == {"100", "200"}
    assert first.scan_memory["last_new_sessions"] == 2
    assert any(call[0] == "many" and call[-1] == 12 for call in calls)

    calls.clear()
    phase["value"] = 2
    second = m.scrape_recu_model(
        "test", settings, cache, threading.Event(), force=True,
        fetch_html_fn=fetch_one, fetch_many_fn=fetch_many,
    )
    assert {item.video_id for item in second.moments} == {"100", "200", "300"}
    assert second.scan_memory["last_new_sessions"] == 1
    assert second.scan_memory["last_stop_boundaries"] >= 1
    assert not any("/page/2" in str(call) for call in calls), calls
    persisted = cache.get_raw("test")
    assert isinstance(persisted.get("scan_memory"), dict)
    assert "300" in persisted["scan_memory"]["known_sessions"]

# Reauth classifier must fail closed for challenge/login/empty-listing states.
dummy = type("Dummy", (), {"config": {"recu": {}}})()
bridge = s.RecuBrowserBridge(dummy)
for body in (
    "<html><title>Just a moment...</title><div id='cf-chl-x'></div></html>",
    "<html><form><input name='password'>Log in</form></html>",
):
    try:
        bridge._validate_recu_html("https://recu.me/performer/x/kinks/cumshot", body, expect_listing=True)
    except s.RecuReauthRequired as exc:
        assert getattr(exc, "reauth_required", False)
    else:
        raise AssertionError("Expected RecuReauthRequired")
try:
    bridge._validate_recu_html("https://recu.me/performer/x/kinks/cumshot", "<html>normal shell only</html>", expect_listing=True)
except s.RecuReauthRequired:
    pass
else:
    raise AssertionError("Unexpected empty listing must request reauth")

server_text = (ROOT / "ctbrec_mobile_server.py").read_text(encoding="utf-8")
app_text = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
assert "Network.loadNetworkResource" in server_text
assert '"includeCredentials": True' in server_text
assert "Network.setBlockedURLs" in server_text and "Page.stopLoading" in server_text
assert "concurrent_requests" in server_text and "12" in server_text
assert "local_disk_url" in server_text
assert "isDesktopPreviewClient" in app_text and "desktopDiskDirect" in app_text
print("PASS v2.14 Recu incremental/CDP/direct-disk regression suite")
