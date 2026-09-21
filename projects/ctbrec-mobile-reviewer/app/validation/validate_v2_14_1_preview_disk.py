#!/usr/bin/env python3
"""v2.14.1 direct-disk desktop preview regression checks."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module

s = load("ctbrec_server_v2141_preview", ROOT / "ctbrec_mobile_server.py")

# Standard/open-ended/suffix ranges. Suffix ranges are critical because browser
# media stacks often fetch the tail of MP4/MOV files to locate metadata.
assert s._parse_http_byte_range("", 1000) == (0, 999)
assert s._parse_http_byte_range("bytes=100-", 1000) == (100, 999)
assert s._parse_http_byte_range("bytes=100-199", 1000) == (100, 199)
assert s._parse_http_byte_range("bytes=-128", 1000) == (872, 999)
assert s._parse_http_byte_range("bytes=-5000", 1000) == (0, 999)
assert s._parse_http_byte_range("bytes=1000-", 1000) is None
assert s._parse_http_byte_range("bytes=-0", 1000) is None

server = (ROOT / "ctbrec_mobile_server.py").read_text(encoding="utf-8")
app = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
assert '"remux_url": base +' in server
assert 'Content-Range", f"bytes */{size}"' in server
assert "return !/(iPhone|iPad|iPod|Android|Mobile)/i.test(ua);" in app
assert "video.dataset.previewStage" in app
assert "item.remux_url" in app and "item.compatibility_url" in app
assert 'previewFailover(reason)' in app and "'startup-timeout'" in app
assert "location.hostname" not in app[app.index("function isDesktopPreviewClient"):app.index("function playPreviewItem")], \
    "desktop original-file preview must not silently disable itself when the same PC uses its Tailscale hostname"
print("PASS v2.14.1 direct-disk desktop preview range/fallback regression suite")
