"""Current transport regression test (updated by v2.13.3).

v2.13.0 experimented with HTTP/1.1 + Python-layer gzip. Real iOS/Tailscale
traffic returned `Failed to fetch`, so v2.13.3 retains the
known-stable HTTP/1.0/plain-JSON transport while preserving streaming files.
"""
from pathlib import Path
import json
import os
import subprocess
import sys
import tempfile
import time
import http.client
import hashlib
import hmac
import shutil

SRC = Path(__file__).resolve().parents[1]

with tempfile.TemporaryDirectory(prefix='ctbrec_v2133_http_') as td:
    td = Path(td)
    # Copy source without the very large model; this transport test does not run inference.
    for item in SRC.iterdir():
        if item.name in {'models', '__pycache__'}:
            continue
        dst = td / item.name
        if item.is_dir():
            shutil.copytree(item, dst)
        else:
            shutil.copy2(item, dst)
    (td / 'models').mkdir(exist_ok=True)
    cfg = json.loads((td / 'mobile_config.json').read_text(encoding='utf-8'))
    cfg['port'] = 18787
    cfg['scan_on_start'] = False
    cfg['access_pin'] = '246810'
    cfg['secret_key'] = 'v2133-transport-test-secret'
    cfg.setdefault('library_background', {})['enabled'] = False
    cfg.setdefault('non_nsfw_cleanup', {})['enabled'] = False
    cfg.setdefault('live_bridge', {})['enabled'] = False
    (td / 'mobile_config.json').write_text(json.dumps(cfg, indent=2), encoding='utf-8')
    (td / 'recording_roots.txt').write_text('', encoding='utf-8')

    proc = subprocess.Popen(
        [sys.executable, str(td / 'ctbrec_mobile_server.py')],
        cwd=td,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.time() + 20
        last = None
        while time.time() < deadline:
            try:
                c = http.client.HTTPConnection('127.0.0.1', 18787, timeout=2)
                c.request('GET', '/api/auth')
                r = c.getresponse(); raw = r.read(); c.close()
                if r.status == 200:
                    auth = json.loads(raw)
                    assert auth['authenticated'] is True
                    break
            except Exception as exc:
                last = exc; time.sleep(.2)
        else:
            raise AssertionError(f'server did not become ready: {last}')

        # Known-stable compatibility transport: HTTP/1.0, no Python-layer gzip.
        c = http.client.HTTPConnection('127.0.0.1', 18787, timeout=5)
        c.request('GET', '/api/bootstrap', headers={'Accept-Encoding': 'gzip'})
        r = c.getresponse(); raw = r.read(); headers = dict(r.getheaders()); version = r.version; c.close()
        assert r.status == 200, r.status
        assert version == 10, f'expected HTTP/1.0 response, got version code {version}'
        assert not headers.get('Content-Encoding'), headers
        json.loads(raw)
        print('PASS stable HTTP/1.0 plain-JSON bootstrap')

        # Paired-device recovery uses a dedicated POST body, not Authorization on every request.
        token = hmac.new(cfg['secret_key'].encode(), b'ctbrec-mobile-device-v1', hashlib.sha256).hexdigest()
        body = json.dumps({'device_token': token})
        c = http.client.HTTPConnection('127.0.0.1', 18787, timeout=5)
        c.request('POST', '/api/auth/recover', body=body, headers={'Content-Type': 'application/json', 'Content-Length': str(len(body.encode()))})
        r = c.getresponse(); payload = json.loads(r.read()); set_cookie = r.getheader('Set-Cookie') or ''; c.close()
        assert r.status == 200 and payload.get('authenticated') is True
        assert 'ctbrec_auth=' in set_cookie
        print('PASS explicit paired-device session recovery endpoint')

        # Static assets still stream correctly with complete Content-Length.
        c = http.client.HTTPConnection('127.0.0.1', 18787, timeout=5)
        c.request('GET', '/static/app.js?v=21510')
        r = c.getresponse(); raw = r.read(); length = int(r.getheader('Content-Length') or 0); c.close()
        assert r.status == 200 and len(raw) == length and b'/api/auth/recover' in raw
        assert b'Authorization' not in raw
        print('PASS streamed current app asset and no global Authorization header')

        # Catalog and diagnostics paths must return quickly even with an empty test catalog.
        c = http.client.HTTPConnection('127.0.0.1', 18787, timeout=5)
        c.request('GET', '/api/catalog?mode=original&filter=&drives=')
        r = c.getresponse(); payload = json.loads(r.read()); c.close()
        assert r.status == 200 and payload.get('models') == [] and 'request_ms' in payload
        print('PASS timed catalog endpoint')

        c = http.client.HTTPConnection('127.0.0.1', 18787, timeout=5)
        c.request('GET', '/api/diagnostics/status')
        r = c.getresponse(); diag = json.loads(r.read()); c.close()
        assert r.status == 200 and diag.get('server_version') == 'CTBRecMobile/2.15.10'
        assert 'last_catalog_request' in diag and 'catalog_counts' in diag
        print('PASS diagnostics status endpoint')
    finally:
        proc.terminate()
        try: proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill(); proc.wait(timeout=5)
