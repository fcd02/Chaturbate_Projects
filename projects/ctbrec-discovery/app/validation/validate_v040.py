from __future__ import annotations
import json, tempfile, threading, time, urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from discovery.server import App, Handler

class LiveH(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith('/api/models'):
            body=json.dumps({'models':[
                {'name':'seedmale','priority':1000,'online':True,'affiliateRoomInfo':{'gender':'m','tags':['athletic'],'num_users':50}},
                {'name':'femalecandidate','priority':50,'online':True,'affiliateRoomInfo':{'gender':'f','tags':['athletic']}},
            ]}).encode()
            self.send_response(200); self.send_header('Content-Type','application/json'); self.send_header('Content-Length',str(len(body))); self.end_headers(); self.wfile.write(body); return
        self.send_response(404); self.end_headers()
    def log_message(self,*args): pass

with tempfile.TemporaryDirectory() as td:
    t=Path(td)
    catalog=t/'mobile_catalog_cache.json'
    catalog.write_text(json.dumps({'updated_at':'x','roots_file':'r','catalog':{
        'seedmale':[{'folder':'E:/seedmale','bytes':100}],
        'malecandidate':[{'folder':'E:/malecandidate','bytes':80}],
        'femalecandidate':[{'folder':'E:/femalecandidate','bytes':70}],
    }}))
    recu=t/'recu_local_archive.json'
    recu.write_text(json.dumps({'version':1,'models':{'malecandidate':{'moments':[{},{}],'video_meta':{'1':{}}}}}))
    live=HTTPServer(('127.0.0.1',0),LiveH); threading.Thread(target=live.serve_forever,daemon=True).start()
    cfg=t/'config.json'
    cfg.write_text(json.dumps({
        'database_path':str(t/'state.sqlite3'),
        'mobile_catalog_cache':str(catalog),
        'reviewer_base_url':'http://127.0.0.1:1',
        'reviewer_probe_timeout_seconds':0.1,
        'live_control_base_url':f'http://127.0.0.1:{live.server_port}',
        'sync_interval_seconds':999,
        'recommendation_filters':{'allowed_genders':['m'],'include_unknown_gender':True,'include_couples':False},
        'collectors':{
            'live_control_models':{'enabled':True,'poll_seconds':60},
            'chaturbate_affiliate':{'enabled':False,'wm':'SECRET_WM_SENTINEL'},
            'recu_local_archives':{'enabled':True,'paths':[str(recu)],'poll_seconds':600},
        }
    }))
    root=Path(__file__).resolve().parents[1]
    start=time.monotonic(); app=App(root,cfg); elapsed=time.monotonic()-start
    max_startup=float(__import__('os').environ.get('CTBREC_VALIDATION_STARTUP_MAX_SECONDS','5.0'))
    assert elapsed < max_startup, f'App construction unexpectedly blocked for {elapsed:.2f}s (limit {max_startup:.2f}s)'
    print(f'v0.4 construction elapsed={elapsed:.3f}s limit={max_startup:.3f}s')
    result=app.engine.sync_local_sources()
    assert result['catalog']['imported']==3, result
    seed=app.engine.store.find_account('chaturbate','seedmale'); assert seed and seed['preference_label']=='favorite'
    rec=app.engine.store.find_account('chaturbate','malecandidate'); assert rec and rec['metadata']['recu_moments']==2
    names={r.username for r in app.engine.recommendations(100)}
    assert 'malecandidate' in names and 'femalecandidate' not in names, names
    # Real HTTP surface smoke.
    srv=ThreadingHTTPServer(('127.0.0.1',0),Handler); srv.app=app; threading.Thread(target=srv.serve_forever,daemon=True).start()
    for path in ('/api/health','/api/diagnostics','/api/settings','/api/recommendations?limit=10'):
        with urllib.request.urlopen(f'http://127.0.0.1:{srv.server_port}{path}',timeout=2) as resp:
            assert resp.status==200; payload=json.loads(resp.read().decode())
            if path == '/api/settings':
                assert 'SECRET_WM_SENTINEL' not in json.dumps(payload), payload
    # Safe Settings writes only recommendation filters and preserves unrelated secrets/config.
    body=json.dumps({'recommendation_filters':{'allowed_genders':['m','f'],'include_unknown_gender':False,'include_couples':False}}).encode()
    req=urllib.request.Request(f'http://127.0.0.1:{srv.server_port}/api/settings',data=body,headers={'Content-Type':'application/json'},method='POST')
    with urllib.request.urlopen(req,timeout=2) as resp:
        assert resp.status==200; updated=json.loads(resp.read().decode()); assert updated['recommendation_filters']['allowed_genders']==['m','f']
        assert 'SECRET_WM_SENTINEL' not in json.dumps(updated), updated
    disk=json.loads(cfg.read_text())
    assert disk['collectors']['chaturbate_affiliate']['wm']=='SECRET_WM_SENTINEL', disk
    assert disk['live_control_base_url'].endswith(str(live.server_port)), disk
    srv.shutdown(); srv.server_close(); live.shutdown(); live.server_close()
print('PASS v0.4 real-cache-shape, Live Control local reuse, Recu enrichment, gender filter, safe-settings secret preservation, nonblocking construction, and HTTP smoke')
