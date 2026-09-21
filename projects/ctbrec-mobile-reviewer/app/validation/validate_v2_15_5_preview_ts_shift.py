#!/usr/bin/env python3
from pathlib import Path
import json, tempfile, threading, zipfile, io, sys
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import ctbrec_mobile_server as server
APP=(ROOT/'static/app.js').read_text(encoding='utf-8')
HTML=(ROOT/'static/index.html').read_text(encoding='utf-8')
SW=(ROOT/'static/service-worker.js').read_text(encoding='utf-8')
SERVER=(ROOT/'ctbrec_mobile_server.py').read_text(encoding='utf-8')

# Version/cache and new UI.
assert 'CTBRecMobile/2.15.10' in SERVER
assert 'ctbrec-shell-v21510' in SW
assert 'app.js?v=21510' in HTML and 'rapid.js?v=21510' in HTML
assert 'data-model-sort="ts_size"' in HTML and 'ts-sort-note' in HTML
assert 'preview-export-diagnostics' in HTML and 'preview-source-timeline' in HTML

# Direct media MIME must not trust OS registry for TS.
assert server.video_content_type(Path('anything.ts')) == 'video/mp2t'
assert server.video_content_type(Path('anything.mp4')) == 'video/mp4'

# Existing catalog scan gets exact .ts subtotal in same directory pass.
with tempfile.TemporaryDirectory() as td:
    d=Path(td)
    (d/'a.ts').write_bytes(b'x'*111)
    (d/'b.mp4').write_bytes(b'y'*222)
    (d/'c.m2ts').write_bytes(b'z'*333)
    total, ts = server.direct_video_byte_breakdown(d, {'.ts','.mp4','.m2ts'})
    assert total == 666, (total, ts)
    assert ts == 111, (total, ts)

# Catalog exposes TS stats and refuses to pretend an old cached row has them.
class DummyAdmin:
    def filter_cache_token(self): return ('x',)
    def hidden_names(self): return set()
    def ignored_names(self): return set()
    def easy_sort_names(self): return set()
state=object.__new__(server.MobileReviewerState)
state.lock=threading.RLock(); state.model_admin=DummyAdmin(); state.action_queue={'jobs':[]}
state.catalog_status={'updated_at':'x'}; state.ready_index={'updated_at':'','snapshots':{}}
state.catalog_view_cache={}; state.ready_count_cache={}; state._ready_counts_for_mode=lambda mode,drives:{}
state.catalog={'original':[
    {'name':'HasTS','bytes':1000,'ts_bytes':700,'folder':'/tmp/a','drive':'E'},
    {'name':'OldCache','bytes':900,'folder':'/tmp/b','drive':'E'},
], 'review':[], 'deletion':[]}
rows=server.MobileReviewerState.catalog_models(state,'original',{'E'},'')
by={r['name']:r for r in rows}
assert by['HasTS']['ts_stats_known'] is True and by['HasTS']['ts_bytes']==700
assert by['OldCache']['ts_stats_known'] is False

# Shift-click now survives browsers/PWAs that lose shiftKey on click after pointerdown.
assert 'function shiftRangeRequested(event = null)' in APP
assert "event?.currentTarget?.dataset?.shiftPointer === '1'" in APP
assert 'rememberPointerShift(hit, event)' in APP and 'rememberPointerShift(row, event)' in APP
assert "window.addEventListener('keydown', event => { if (event.key === 'Shift') state.shiftKeyDown = true; });" in APP
assert 'shiftRangeRequested(event) && state.lastFileSelectionIndex !== null' in APP
assert 'shiftRangeRequested(event) && state.lastFrameSelectionIndex !== null' in APP

# Preview fixes: metadata alone must not cancel the watchdog; direct MIME, nonseekable
# fallback seek restarts, diagnostics telemetry/export all present.
assert 'Do NOT clear the startup watchdog here' in APP
assert "armPreviewFallbackTimer(6000, 'seek-timeout')" in APP
assert 'seekCurrentPreviewToAbsolute' in APP and 'previewUsesSourceTimeline' in APP
assert '/api/preview-diagnostics/event' in APP and '/api/preview-diagnostics/export' in APP
assert 'video_content_type(path)' in SERVER
assert 'server_range' in SERVER and 'ffprobe.json' in SERVER

# Diagnostic ZIP contains metadata/log only, not media bytes.
state2=object.__new__(server.MobileReviewerState)
state2.lock=threading.RLock(); state2.preview_diagnostics={}; state2.preview_diagnostics_order=[]
state2._preview_settings=lambda: {'test':True}
state2.preview_probe=lambda path: {'ok':True,'format':{'format_name':'mpegts'}}
with tempfile.TemporaryDirectory() as td:
    media=Path(td)/'clip.ts'; media.write_bytes(b'MEDIASECRET'*100)
    state2.preview_diagnostics['abc']={
        'diagnostic_id':'abc','path':str(media),'events':[{'event':'x'}],'name':'clip.ts'
    }
    raw=server.MobileReviewerState.preview_diagnostic_zip(state2,'abc','UnitTest')
    z=zipfile.ZipFile(io.BytesIO(raw))
    names=set(z.namelist())
    assert {'preview_summary.json','ffprobe.json','mobile_reviewer_log_tail.txt','README.txt'} <= names
    combined=b''.join(z.read(n) for n in z.namelist())
    assert b'MEDIASECRET' not in combined

print('PASS: v2.15.6 preview reliability, TS-only size metadata, diagnostics, and robust Shift-click')

# Real Chromium modifier path: pointerdown + Shift key must select the inclusive file range.
from playwright.sync_api import sync_playwright
html = HTML
for tag in ('<script src="/static/app.js?v=21510"></script>','<script src="/static/rapid.js?v=21510"></script>','<link rel="stylesheet" href="/static/styles.css?v=21510">','<link rel="manifest" href="/static/manifest.webmanifest">'):
    html = html.replace(tag, '')
mock = r'''
(() => {
  const response=(obj,status=200)=>new Response(JSON.stringify(obj),{status,headers:{'Content-Type':'application/json'}});
  window.fetch=async(input,opts={})=>{
    const p=new URL(String(input),'http://mock.local').pathname;
    if(p==='/api/auth') return response({authenticated:true,device_token:'x'});
    if(p==='/api/bootstrap') return response({drives:['E'],catalog_status:{status:'ready',updated_at:'x'},library_background:{},non_nsfw_background:{},background_scheduler:{},action_queue:{}});
    if(p==='/api/catalog') return response({models:[],status:{updated_at:'x'}});
    if(p==='/api/background-status') return response({catalog_status:{updated_at:'x'},library_background:{},non_nsfw_background:{},background_scheduler:{},action_queue:{}});
    if(p==='/api/sorting/heartbeat') return response({ok:true});
    return response({ok:true});
  };
  try { Object.defineProperty(navigator,'serviceWorker',{value:undefined,configurable:true}); } catch(_) {}
})();
'''
with sync_playwright() as pw:
    browser=pw.chromium.launch(headless=True, executable_path='/usr/bin/chromium', args=['--no-sandbox'])
    page=browser.new_page(viewport={'width':1000,'height':800})
    page.set_content(html, wait_until='domcontentloaded')
    page.add_script_tag(content=mock)
    page.add_script_tag(content=APP)
    page.evaluate("""() => {
      state.current={done:false,mode:'original',model:'SHIFT',chunk:{signature:'s',files:[0,1,2,3,4].map(i=>({index:i,number:i+1,name:`f${i}.ts`,size:'1 MB',bytes:1,kinks:[]})),parts:[{url:'data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw==',tiles:[0,1,2,3,4].map(i=>({file_index:i,number:i+1,left:i*20,top:0,width:20,height:100}))}]}};
      state.keep=new Set(); state.lastFileSelectionIndex=null; renderMosaics(); renderFiles();
      document.querySelector('#review-view').classList.remove('hidden');
    }""")
    page.locator('#mosaic-list .tile-hit[data-file-index="1"]').click()
    page.keyboard.down('Shift')
    page.locator('#mosaic-list .tile-hit[data-file-index="4"]').click()
    page.keyboard.up('Shift')
    selected=page.evaluate('Array.from(state.keep).sort((a,b)=>a-b)')
    assert selected == [1,2,3,4], selected
    browser.close()
print('PASS: real Chromium Shift-click selects inclusive range')
