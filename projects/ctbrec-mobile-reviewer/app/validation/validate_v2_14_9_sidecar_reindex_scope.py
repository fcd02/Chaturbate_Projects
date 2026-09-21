#!/usr/bin/env python3
"""v2.14.9 regression: sidecar recovery is conditional and status is truthful."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
server = (ROOT / 'ctbrec_mobile_server.py').read_text(encoding='utf-8')
app = (ROOT / 'static' / 'app.js').read_text(encoding='utf-8')
sw = (ROOT / 'static' / 'service-worker.js').read_text(encoding='utf-8')
html = (ROOT / 'static' / 'index.html').read_text(encoding='utf-8')

assert 'server_version = "CTBRecMobile/2.15.10"' in server
assert 'ctbrec-shell-v21510' in sw
assert 'app.js?v=21510' in html and 'rapid.js?v=21510' in html

start = server.index('    def prioritize_model_mosaics')
end = server.index('    def open_model_sort_first', start)
priority = server[start:end]
assert 'sidecar_count > loaded_ready' in priority
assert '_existing_mosaic_sidecar_count' in priority
assert 'no sidecar reindex needed' in priority
assert priority.index('sidecar_count > loaded_ready') < priority.index('_recover_partial_snapshot_from_existing_mosaics')

merge_start = server.index('    def _merge_ready_chunks_into_queue')
merge_end = server.index('    def _merge_model_chunks_into_queue', merge_start)
merge = server[merge_start:merge_end]
assert 'if additions:' in merge
assert 'Recovered {len(additions)} additional valid existing mosaic(s)' in merge
assert 'Existing mosaics were reindexed.' not in merge

# Existing compatible mosaics remain capability/metadata validated rather than
# invalidated by the software version that created them.
embedded_start = server.index('    def _embedded_layout_entry')
embedded_end = server.index('    def _review_frame_timeline', embedded_start)
embedded = server[embedded_start:embedded_end]
assert 'int(layout.get("version", 0) or 0) < 2' in embedded
assert 'layout_mode' in embedded and 'signature' in embedded
assert '2.14.' not in embedded, 'mosaic reuse must not be tied to release version'

# User-facing open message only says reindexed when quick-load truly recovered
# additional work synchronously; the background status no longer lies.
assert 'Reindexed ${recovered} additional existing mosaic' in app
print('PASS v2.14.9 scoped sidecar recovery / truthful status / method-compatible reuse regression')
