from __future__ import annotations

import importlib.util
import json
import tempfile
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from discovery import __version__
from discovery.importers import import_catalog
from discovery.store import DiscoveryStore
from discovery.server import App, Handler

assert __version__ == '0.4.6', __version__

# Validation architecture: only manifest-selected current suites execute automatically.
runner_path = ROOT / 'validation' / 'runner.py'
spec = importlib.util.spec_from_file_location('ctbrec_validation_runner', runner_path)
assert spec and spec.loader
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)
manifest = runner.load_manifest(ROOT)
assert manifest['release_version'] == __version__, manifest
selected = [p.name for p in runner.selected_release_suites(ROOT)]
assert selected == ['validate_current.py'], selected
historical = {p.name for p in (ROOT / 'validation').glob('validate_v*.py')}
# Historical validators may be present OR absent. If present, none may become authoritative implicitly.
assert not (historical & set(selected)), (historical, selected)

bridge_path = ROOT / 'tools' / 'git_bridge' / 'bridge.py'
spec = importlib.util.spec_from_file_location('ctbrec_git_bridge_current', bridge_path)
assert spec and spec.loader
bridge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge)
assert bridge.BRIDGE_VERSION == '1.2.0', bridge.BRIDGE_VERSION
patch_manifest = json.loads((ROOT / 'PATCH_MANIFEST.json').read_text(encoding='utf-8'))
assert patch_manifest['patch_type'] == 'cumulative_public_source_overlay', patch_manifest
assert patch_manifest['release_version'] == __version__, patch_manifest
source_manifest = json.loads((ROOT / 'validation' / 'source_manifest.json').read_text(encoding='utf-8'))
assert source_manifest['release_version'] == __version__, source_manifest
assert 'discovery/importers.py' in source_manifest['files'], 'canonical manifest must pin importer bytes'
bridge_src = bridge_path.read_text(encoding='utf-8')
assert 'validation/runner.py + release_manifest.json' in bridge_src
assert 'glob("validate_*.py")' not in bridge_src

# Current nested-catalog behavior, independently of stale v0.4.1 expectations.
with tempfile.TemporaryDirectory() as td:
    t = Path(td)
    db = t / 'state.sqlite3'
    store = DiscoveryStore(db)
    for bogus in ('original', 'review', 'deletion'):
        store.upsert_identity_account('chaturbate', bogus, source='mobile_reviewer_catalog')
    catalog = t / 'mobile_catalog_cache.json'
    catalog.write_text(json.dumps({
        'updated_at': '2026-09-20T19:00:00Z',
        'roots_file': 'recording_roots.txt',
        'catalog': {
            'original': [
                {'model_name': 'alpha', 'folder': 'E:/Recordings/alpha', 'bytes': 100},
                {'model_name': 'beta', 'folder': 'F:/Recordings/beta', 'bytes': 200},
            ],
            'review': {
                'alpha': [{'folder': 'E:/Recordings/alpha/Review', 'bytes': 30}],
                'gamma': [{'folder': 'G:/Recordings/gamma/Review', 'bytes': 40}],
            },
            'deletion': {
                'G:/MARKED_FOR_DELETION': {
                    'delta': [{'folder': 'G:/MARKED_FOR_DELETION/delta', 'bytes': 50}]
                }
            },
        }
    }), encoding='utf-8')
    result = import_catalog(catalog, store)
    assert result['seen'] == 5, result
    assert result['imported'] == 4, result
    assert result['cleaned_artifacts'] == 3, result
    names = {x['username'] for x in store.list_accounts()}
    assert {'alpha', 'beta', 'gamma', 'delta'}.issubset(names), names
    assert not {'original', 'review', 'deletion'}.intersection(names), names

# Current application/HTTP smoke with secrets kept out of APIs.
class LiveH(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith('/api/models'):
            body = json.dumps({'models': [
                {'name': 'seedmale', 'priority': 1000, 'online': True, 'affiliateRoomInfo': {'gender': 'm', 'tags': ['athletic']}},
                {'name': 'malecandidate', 'priority': 50, 'online': True, 'affiliateRoomInfo': {'gender': 'm', 'tags': ['athletic']}},
                {'name': 'femalecandidate', 'priority': 50, 'online': True, 'affiliateRoomInfo': {'gender': 'f', 'tags': ['athletic']}},
            ]}).encode()
            self.send_response(200); self.send_header('Content-Type', 'application/json'); self.send_header('Content-Length', str(len(body))); self.end_headers(); self.wfile.write(body); return
        self.send_response(404); self.end_headers()
    def log_message(self, *args): pass

class ReviewerH(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/api/auth':
            body = b'{"authenticated":true,"local":true}'
            self.send_response(200); self.send_header('Content-Length', str(len(body))); self.end_headers(); self.wfile.write(body); return
        self.send_response(503); self.end_headers()
    def do_HEAD(self): self.send_response(501); self.end_headers()
    def log_message(self, *args): pass

with tempfile.TemporaryDirectory() as td:
    t = Path(td)
    catalog = t / 'mobile_catalog_cache.json'
    catalog.write_text(json.dumps({'updated_at':'x','roots_file':'r','catalog':{
        'seedmale':[{'folder':'E:/seedmale','bytes':100}],
        'malecandidate':[{'folder':'E:/malecandidate','bytes':80}],
        'femalecandidate':[{'folder':'E:/femalecandidate','bytes':70}],
    }}), encoding='utf-8')
    recu = t / 'recu_local_archive.json'
    recu.write_text(json.dumps({'version':1,'models':{'malecandidate':{'moments':[{},{}],'video_meta':{'1':{}}}}}), encoding='utf-8')
    live = HTTPServer(('127.0.0.1', 0), LiveH); threading.Thread(target=live.serve_forever, daemon=True).start()
    reviewer = HTTPServer(('127.0.0.1', 0), ReviewerH); threading.Thread(target=reviewer.serve_forever, daemon=True).start()
    cfg = t / 'config.json'
    cfg.write_text(json.dumps({
        'database_path': str(t/'state.sqlite3'),
        'mobile_catalog_cache': str(catalog),
        'reviewer_base_url': f'http://127.0.0.1:{reviewer.server_port}',
        'reviewer_probe_timeout_seconds': 1.0,
        'live_control_base_url': f'http://127.0.0.1:{live.server_port}',
        'sync_interval_seconds': 999,
        'recommendation_filters': {'allowed_genders':['m'], 'include_unknown_gender':True, 'include_couples':False},
        'collectors': {
            'live_control_models': {'enabled':True, 'poll_seconds':60},
            'chaturbate_affiliate': {'enabled':False, 'wm':'SECRET_WM_SENTINEL'},
            'recu_local_archives': {'enabled':True, 'paths':[str(recu)], 'poll_seconds':600},
        }
    }), encoding='utf-8')
    app = App(ROOT, cfg)
    sync = app.engine.sync_local_sources()
    assert sync['catalog']['imported'] == 3, sync
    rec = app.engine.store.find_account('chaturbate', 'malecandidate')
    assert rec and rec['metadata']['recu_moments'] == 2, rec
    live_result = app.engine.collectors.run('live_control_models', force=True)
    assert live_result['status'] == 'ok', live_result
    integration = app.engine.integration.status()
    assert integration['reviewer']['reachable'] is True, integration
    names = {r.username for r in app.engine.recommendations(100)}
    assert 'malecandidate' in names and 'femalecandidate' not in names, names

    srv = ThreadingHTTPServer(('127.0.0.1', 0), Handler); srv.app = app; threading.Thread(target=srv.serve_forever, daemon=True).start()
    for path in ('/api/health','/api/diagnostics','/api/settings','/api/recommendations?limit=10','/api/collectors'):
        with urllib.request.urlopen(f'http://127.0.0.1:{srv.server_port}{path}', timeout=2) as resp:
            assert resp.status == 200
            payload = json.loads(resp.read().decode())
            assert 'SECRET_WM_SENTINEL' not in json.dumps(payload), (path, payload)
    srv.shutdown(); srv.server_close(); live.shutdown(); live.server_close(); reviewer.shutdown(); reviewer.server_close()

print('PASS v0.4.6 authoritative-current validation, nested catalog behavior, local-source integration, filter semantics, secret boundary, HTTP smoke, and Git Bridge v1.2.0 cumulative-patch contract')
