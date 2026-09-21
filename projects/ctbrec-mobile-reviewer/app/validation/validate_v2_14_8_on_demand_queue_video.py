#!/usr/bin/env python3
"""v2.14.8 regression: exact-model open, bounded on-demand queue work, and preview failover."""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
server = (ROOT / "ctbrec_mobile_server.py").read_text(encoding="utf-8")
app = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
rapid = (ROOT / "static" / "rapid.js").read_text(encoding="utf-8")
html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
sw = (ROOT / "static" / "service-worker.js").read_text(encoding="utf-8")

# Release identity / cache busting.
assert 'server_version = "CTBRecMobile/2.15.10"' in server
assert "ctbrec-shell-v21510" in sw
assert "app.js?v=21510" in html and "rapid.js?v=21510" in html

# Exact model requested by user must open without the old 12-second abort or
# automatic suggestion hop. Both standard and rapid-sort overrides must use
# the same no-deadline open-fast request.
def block(text: str, start: str, end: str) -> str:
    a = text.index(start)
    b = text.index(end, a)
    return text[a:b]

open_block = block(app, "async function openModel", "async function nextReadyModel")
rapid_open = block(rapid, "openModel = async function openModelRapid", "submitCurrent = async function submitCurrentRapid")
assert "timeoutMs: 0" in open_block
assert "result.suggestion" not in open_block and "suggestions" not in open_block
assert "await nextReadyModel()" not in app, "automatic next-ready model hop reintroduced"
assert "timeoutMs: 0" in rapid_open
assert "result.suggestion" not in rapid_open and "suggestions" not in rapid_open
assert "pending" in open_block and "automatic look-ahead" in open_block.lower()
assert "pendingModel" in app and "submit-button').disabled = pendingModel" in app

# Server open-fast must deliberately skip synchronous sidecar reconciliation,
# create a pending queue if nothing is ready, and keep the exact clicked model.
server_open = block(server, "    def open_model_sort_first", "    def _work_token")
assert "recover_sidecars=False" in server_open
assert '"preparing_model": True' in server_open
assert '"model": model_name' in server_open
assert "suggest" not in server_open.casefold()

# v2.15.2 intentionally restores *bounded active-model* look-ahead, but must
# preserve v2.14.8's no-auto-hop and leave-model cancellation boundaries.
priority = block(server, "    def prioritize_model_mosaics", "    def open_model_sort_first")
assert '"policy": "current-plus-bounded-lookahead"' in priority
assert "self.request_interactive_mosaic(target, False)" in priority
assert "self.schedule_prefetch(target)" in priority
assert "for index, chunk in enumerate(chunks" not in priority

# Leaving/switching queue must cancel its hidden priority/prefetch demand.
assert "def _deactivate_queue_work_locked" in server
assert 'queue_data["prefetch_cancel_requested"] = True' in server
assert "record.cancel_requested = True" in server
assert "self._deactivate_queue_work_locked(stale_queue_id)" in server and "self._deactivate_queue_work_locked(queue_id)" in server
assert "Stopped mosaic generation because this model is no longer open on the phone." in server
assert "Generate Ahead stopped because this model is no longer open." in server

# Preview failures should progress automatically through direct/HLS -> seekable
# zero-reencode cache -> compatibility, including sources that stall without firing an error event.
preview = block(app, "function playPreviewItem", "function closeVideoPreview")
assert 'previewFailover(reason)' in preview and "'startup-timeout'" in preview
assert "item.seekable_url" in preview and "item.compatibility_url" in preview
assert "armPreviewFallbackTimer(stage === 'hls' ? 10000" in preview
assert "video.onerror = () => previewFailover('media-error')" in preview
assert "seekable zero-reencode MP4 wrapper" in preview and "trying compatibility playback automatically" in preview

# FFmpeg preview paths are more tolerant of broken timestamps/truncated chunks,
# and HLS segment generation cannot hang forever.
assert "+genpts+discardcorrupt" in server
assert "ignore_err" in server
assert "avoid_negative_ts" in server
assert "default_base_moof" in server
assert "timeout_seconds = max(30, int(length * 8 + 20))" in server

print("PASS v2.14.8 on-demand queue / exact model open / preview failover regression suite")
