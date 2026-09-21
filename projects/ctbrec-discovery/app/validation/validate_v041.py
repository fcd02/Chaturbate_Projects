from __future__ import annotations
import json, tempfile, threading, time, urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from discovery.server import App, Handler
from discovery.collectors import CollectorManager

class LiveH(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith('/api/models'):
            body=json.dumps({'models':[
                {'name':'seedmale','priority':1000,'online':True,'affiliateRoomInfo':{'gender':'m','tags':['athletic'],'num_users':50}},
                {'name':'malecandidate','priority':50,'online':True,'affiliateRoomInfo':{'gender':'m','tags':['athletic']}},
                {'name':'femalecandidate','priority':50,'online':True,'affiliateRoomInfo':{'gender':'f','tags':['athletic']}},
            ]}).encode()
            self.send_response(200); self.send_header('Content-Type','application/json'); self.send_header('Content-Length',str(len(body))); self.end_headers(); self.wfile.write(body); return
        self.send_response(404); self.end_headers()
    def log_message(self,*args): pass

class ReviewerH(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/api/auth':
            body=b'{"authenticated":true,"local":true}'
            self.send_response(200); self.send_header('Content-Length',str(len(body))); self.end_headers(); self.wfile.write(body); return
        # Deliberately fail shell GET: v0.4.1 should never need it for health.
        self.send_response(503); self.end_headers()
    def do_HEAD(self): self.send_response(501); self.end_headers()
    def log_message(self,*args): pass

with tempfile.TemporaryDirectory() as td:
    t=Path(td)
    catalog=t/'mobile_catalog_cache.json'
    catalog.write_text(json.dumps({'updated_at':'x','roots_file':'r','catalog':{
        'original':[
            {'model_name':'seedmale','folder':'E:/seedmale','bytes':100},
            {'model_name':'malecandidate','folder':'E:/malecandidate','bytes':80},
        ],
        'review':{
            'seedmale':[{'folder':'E:/seedmale/Review','bytes':20}],
            'femalecandidate':[{'folder':'E:/femalecandidate/Review','bytes':70}],
        },
        'deletion':{'G:/MARKED_FOR_DELETION':{'olddeleted':[{'folder':'G:/MARKED_FOR_DELETION/olddeleted','bytes':10}]}}
    }}))
    recu=t/'recu_local_archive.json'
    recu.write_text(json.dumps({'version':1,'models':{'malecandidate':{'moments':[{},{}],'video_meta':{'1':{}}}}}))
    live=HTTPServer(('127.0.0.1',0),LiveH); threading.Thread(target=live.serve_forever,daemon=True).start()
    reviewer=HTTPServer(('127.0.0.1',0),ReviewerH); threading.Thread(target=reviewer.serve_forever,daemon=True).start()
    cfg=t/'config.json'
    cfg.write_text(json.dumps({
        'database_path':str(t/'state.sqlite3'),
        'mobile_catalog_cache':str(catalog),
        'reviewer_base_url':f'http://127.0.0.1:{reviewer.server_port}',
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
    start=time.monotonic(); app=App(ROOT,cfg); elapsed=time.monotonic()-start
    assert elapsed < 0.5, f'App construction unexpectedly blocked for {elapsed:.2f}s'

    # Seed the exact harmless three-account artifact v0.4.0 could have left behind.
    for bogus in ('original','review','deletion'):
        app.engine.store.upsert_identity_account('chaturbate',bogus,source='mobile_reviewer_catalog')
    result=app.engine.sync_local_sources()
    assert result['catalog']['imported']==4, result['catalog']
    assert result['catalog']['cleaned_artifacts']==3, result['catalog']
    names={a['username'] for a in app.engine.store.list_accounts()}
    assert {'seedmale','malecandidate','femalecandidate','olddeleted'}.issubset(names), names
    assert not {'original','review','deletion'}.intersection(names), names

    seed=app.engine.store.find_account('chaturbate','seedmale'); assert seed and seed['preference_label']=='favorite', seed
    rec=app.engine.store.find_account('chaturbate','malecandidate'); assert rec and rec['metadata']['recu_moments']==2, rec
    names={r.username for r in app.engine.recommendations(100)}
    assert 'malecandidate' in names and 'femalecandidate' not in names, names

    integration=app.engine.integration.status()
    assert integration['reviewer']['reachable'] is True and integration['reviewer']['probe_method']=='GET /api/auth', integration
    assert integration['catalog_file']['catalog_outer_bucket_count']==3, integration
    assert integration['catalog_file']['catalog_unique_model_count']==4, integration

    # A separate collector lock must not block Live Control just because Recu is already running.
    m=app.engine.collectors
    m._locks['recu_local_archives'].acquire()
    try:
        live_result=m.run('live_control_models',force=True)
    finally:
        m._locks['recu_local_archives'].release()
    assert live_result['status']=='ok', live_result

    # Real HTTP surface smoke.
    srv=ThreadingHTTPServer(('127.0.0.1',0),Handler); srv.app=app; threading.Thread(target=srv.serve_forever,daemon=True).start()
    for path in ('/api/health','/api/diagnostics','/api/settings','/api/recommendations?limit=10','/api/collectors'):
        with urllib.request.urlopen(f'http://127.0.0.1:{srv.server_port}{path}',timeout=2) as resp:
            assert resp.status==200; payload=json.loads(resp.read().decode())
            assert 'SECRET_WM_SENTINEL' not in json.dumps(payload), (path,payload)
    srv.shutdown(); srv.server_close(); live.shutdown(); live.server_close(); reviewer.shutdown(); reviewer.server_close()
print('PASS v0.4.1 nested Mobile Reviewer catalog, v0.4 artifact cleanup, Recu forced reparse/match, per-source collector locks, lightweight Reviewer health, filters, and HTTP smoke')
