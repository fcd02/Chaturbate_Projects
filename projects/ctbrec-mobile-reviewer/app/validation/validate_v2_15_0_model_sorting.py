#!/usr/bin/env python3
"""v2.15.0 regression: client-only random size filtering + READY-backed avg chunk sorting."""
from pathlib import Path
import sys, threading

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import ctbrec_mobile_server as server

app = (ROOT / 'static' / 'app.js').read_text(encoding='utf-8')
html = (ROOT / 'static' / 'index.html').read_text(encoding='utf-8')
sw = (ROOT / 'static' / 'service-worker.js').read_text(encoding='utf-8')
server_text = (ROOT / 'ctbrec_mobile_server.py').read_text(encoding='utf-8')

assert 'server_version = "CTBRecMobile/2.15.10"' in server_text
assert 'ctbrec-shell-v21510' in sw
assert 'app.js?v=21510' in html and 'rapid.js?v=21510' in html
assert 'data-model-sort="random"' in html
assert 'data-model-sort="avg_chunk"' in html
assert 'random-min-size-gb' in html and 'random-max-size-gb' in html and 'random-reshuffle' in html
assert "const MODEL_SORT_PREF_KEY = 'ctbrec_model_sort_v2150'" in app
assert 'deterministicRandomRank' in app
assert "sortMode === 'random'" in app and "sortMode === 'avg_chunk'" in app
assert 'This mode never scans disks or generates mosaics.' in app

class DummyAdmin:
    def filter_cache_token(self): return ('admin', 1)
    def hidden_names(self): return set()
    def ignored_names(self): return set()
    def easy_sort_names(self): return set()

state = object.__new__(server.MobileReviewerState)
state.lock = threading.RLock()
state.catalog = {
    'original': [
        {'name':'Alpha','bytes':1000,'folder':'/tmp/a','drive':'E'},
        {'name':'Beta','bytes':900,'folder':'/tmp/b','drive':'E'},
    ],
    'review': [], 'deletion': [],
}
state.catalog_status = {'updated_at':'cat1'}
state.ready_index = {
    'updated_at':'ready1',
    'snapshots': {
        'original:alpha': {
            'mode':'original','model':'Alpha','bytes':1000,'drives':['E'],'updated_at':'snap1',
            'chunks':[{'source_bytes':700},{'source_bytes':300}],
        },
        # Wrong byte total is intentionally stale and must not be trusted.
        'original:beta': {
            'mode':'original','model':'Beta','bytes':800,'drives':['E'],'updated_at':'snap2',
            'chunks':[{'source_bytes':800}],
        },
    },
}
state.catalog_view_cache = {}
state.ready_count_cache = {}
state.model_admin = DummyAdmin()
state.action_queue = {'jobs': []}
state._ready_counts_for_mode = lambda mode, drives: {'alpha':2, 'beta':1}

rows = server.MobileReviewerState.catalog_models(state, 'original', {'E'}, '')
by_name = {row['name']: row for row in rows}
assert by_name['Alpha']['chunk_stats_known'] is True
assert by_name['Alpha']['chunk_count'] == 2
assert by_name['Alpha']['avg_chunk_bytes'] == 500
assert by_name['Beta']['chunk_stats_known'] is False
assert by_name['Beta']['avg_chunk_bytes'] == 0

# A READY-index update must invalidate the short catalog view cache because the
# average-chunk sort metadata comes from that index. No disk work is involved.
state.ready_index['updated_at'] = 'ready2'
state.ready_index['snapshots']['original:beta'] = {
    'mode':'original','model':'Beta','bytes':900,'drives':['E'],'updated_at':'snap3',
    'chunks':[{'source_bytes':600},{'source_bytes':300}],
}
rows2 = server.MobileReviewerState.catalog_models(state, 'original', {'E'}, '')
by_name2 = {row['name']: row for row in rows2}
assert by_name2['Beta']['chunk_stats_known'] is True
assert by_name2['Beta']['avg_chunk_bytes'] == 450

# Different drive scope must not reuse stats from a broader/different snapshot.
state.catalog = {'original':[{'name':'Alpha','bytes':1000,'folder':'/tmp/a','drive':'F'}], 'review':[], 'deletion':[]}
state.catalog_status = {'updated_at':'cat2'}
state.ready_count_cache = {}
rows3 = server.MobileReviewerState.catalog_models(state, 'original', {'F'}, '')
assert rows3[0]['chunk_stats_known'] is False

print('PASS v2.15.0 random size-filter UI + non-invasive READY-backed average-chunk metadata')
