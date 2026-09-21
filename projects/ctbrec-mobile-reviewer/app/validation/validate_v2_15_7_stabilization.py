#!/usr/bin/env python3
from pathlib import Path
import stat
import tempfile
import threading
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import ctbrec_mobile_server as server

APP = (ROOT / 'static' / 'app.js').read_text(encoding='utf-8')
RAPID = (ROOT / 'static' / 'rapid.js').read_text(encoding='utf-8')
HTML = (ROOT / 'static' / 'index.html').read_text(encoding='utf-8')
SERVER = (ROOT / 'ctbrec_mobile_server.py').read_text(encoding='utf-8')

# ---------------------------------------------------------------------------
# Selection/draft invariants
# ---------------------------------------------------------------------------
assert 'id="clear-review-selections"' in HTML
assert "$('clear-review-selections').onclick = clearCurrentSelections;" in APP
assert "state.frameDecisions = {};" in APP
assert "state.lastFrameSelectionIndex = null;" in APP
assert "state.lastFileSelectionIndex = null;" in APP
assert 'draftPayloadForCurrentChunk' in APP
assert '_chunk_signature' in APP and '_chunk_signature' in SERVER
assert 'cancelPendingDraftSave();' in RAPID
assert 'state.frameDecisions = current.draft?.frame_decisions || {};' in RAPID
assert 'state.recoverSelected = new Set((current.draft?.restore_indices || []).map(Number));' in RAPID
assert 'if (chunkChanged && typeof scrollReviewToTop' in RAPID

# Server must reject a stale delayed autosave rather than storing it under the
# newly-current chunk.
class Chunk:
    def __init__(self, signature): self.signature = signature
state = object.__new__(server.MobileReviewerState)
state.lock = threading.RLock()
state.queues = {'q': {'chunks': [Chunk('new')], 'drafts': {}}}
state.current_chunk = lambda queue: queue['chunks'][0] if queue.get('chunks') else None
assert server.MobileReviewerState.save_draft(state, 'q', {'_chunk_signature':'old','frame_decisions':{'1':'Cumshots'}}) is False
assert state.queues['q']['drafts'] == {}
assert server.MobileReviewerState.save_draft(state, 'q', {'_chunk_signature':'new','frame_decisions':{'1':'Cumshots'}}) is True
assert state.queues['q']['drafts']['new'] == {'frame_decisions':{'1':'Cumshots'}}

# ---------------------------------------------------------------------------
# Duration invariants
# ---------------------------------------------------------------------------
# Validated original mosaic sidecars contain the exact duration used to build
# the mosaic. Recovery must prefer it to misleading _tail_ semantics.
assert 'sidecar_duration = float(item.get("duration", 0) or 0)' in SERVER
assert '"mosaic-sidecar" if sidecar_duration > 0' in SERVER
assert 'duration = float(cached or sidecar_duration or tail or 0)' in SERVER

# Preview duration must probe media itself. A filename with a 1h35m tail tag
# must not dictate a 15-minute segment's scrub limit.
with tempfile.TemporaryDirectory() as td:
    td = Path(td)
    media = td / 'model_2026-09-20_12-00-00_tail_1h35m00s_est.ts'
    media.write_bytes(b'x' * 64)
    ffprobe = td / 'ffprobe'
    ffprobe.write_text('#!/bin/sh\nprintf \'%s\\n\' \'{"format":{"duration":"900.25"},"streams":[{"duration":"900.20"}]}\'\n', encoding='utf-8')
    ffprobe.chmod(ffprobe.stat().st_mode | stat.S_IXUSR)
    ffmpeg = td / 'ffmpeg'
    ffmpeg.write_text('#!/bin/sh\nexit 1\n', encoding='utf-8')
    ffmpeg.chmod(ffmpeg.stat().st_mode | stat.S_IXUSR)

    class Review:
        def resolve_ffmpeg(self, settings): return ffmpeg, ffprobe
    probe_state = object.__new__(server.MobileReviewerState)
    probe_state.lock = threading.RLock()
    probe_state.preview_duration_cache = {}
    probe_state.review = Review()
    probe_state.review_settings = {}
    duration = server.MobileReviewerState.preview_exact_duration(probe_state, media)
    assert 900.0 < duration < 901.0, duration
    # Explicit fallback is ignored when real media metadata is available.
    assert server.MobileReviewerState.preview_exact_duration(probe_state, media, 5700, allow_fallback=True) == duration

assert 'actual_duration = self.preview_exact_duration(Path(video.path))' in SERVER
assert '"duration_known": bool(actual_duration > 0)' in SERVER
assert 'finished: duration > 0 && absolute >=' in APP

# ---------------------------------------------------------------------------
# Real browser interaction: symmetric Shift ranges + Rapid reset contract.
# ---------------------------------------------------------------------------
from playwright.sync_api import sync_playwright
html = HTML
for tag in (
    '<script src="/static/app.js?v=21510"></script>',
    '<script src="/static/rapid.js?v=21510"></script>',
    '<link rel="stylesheet" href="/static/styles.css?v=21510">',
    '<link rel="manifest" href="/static/manifest.webmanifest">',
):
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
    if(p.includes('/draft')) return response({ok:true,accepted:true});
    return response({ok:true});
  };
  try { Object.defineProperty(navigator,'serviceWorker',{value:undefined,configurable:true}); } catch(_) {}
})();
'''
with sync_playwright() as pw:
    browser = pw.chromium.launch(headless=True, executable_path='/usr/bin/chromium', args=['--no-sandbox'])
    page = browser.new_page(viewport={'width':1200,'height':900})
    page.set_content(html, wait_until='domcontentloaded')
    page.add_script_tag(content=mock)
    page.add_script_tag(content=APP)

    # Original/file mode: select 1..4, toggle anchor 4 off, then Shift-click 2
    # to clear 2..4 while keeping 1.
    page.evaluate("""() => {
      state.current={done:false,mode:'original',model:'SHIFT',draft:{},initial_count:1,remaining:1,can_back:false,chunk:{signature:'s1',size:'5 MB',file_count:5,files:[0,1,2,3,4].map(i=>({index:i,number:i+1,name:`f${i}.ts`,size:'1 MB',bytes:1,kinks:[]})),parts:[{url:'data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw==',tiles:[0,1,2,3,4].map(i=>({file_index:i,number:i+1,left:i*20,top:0,width:20,height:100}))}]},recu:{status:'disabled',segments:{}},prefetch:{},mosaic_status:{state:'ready'},action_queue:{},library_background:{},non_nsfw_background:{},background_scheduler:{}};
      state.keep=new Set(); state.lastFileSelectionIndex=null; renderMosaics(); renderFiles(); document.querySelector('#review-view').classList.remove('hidden');
    }""")
    page.locator('#mosaic-list .tile-hit[data-file-index="1"]').click()
    page.keyboard.down('Shift'); page.locator('#mosaic-list .tile-hit[data-file-index="4"]').click(); page.keyboard.up('Shift')
    assert page.evaluate('Array.from(state.keep).sort((a,b)=>a-b)') == [1,2,3,4]
    page.locator('#mosaic-list .tile-hit[data-file-index="4"]').click()  # anchor is now unselected
    page.keyboard.down('Shift'); page.locator('#mosaic-list .tile-hit[data-file-index="2"]').click(); page.keyboard.up('Shift')
    assert page.evaluate('Array.from(state.keep).sort((a,b)=>a-b)') == [1]

    # Frame Cut: same symmetric range behavior, then the explicit clear button.
    page.evaluate("""() => {
      state.current={done:false,mode:'review',model:'FRAME',draft:{},initial_count:1,remaining:1,can_back:false,chunk:{signature:'r1',size:'1 MB',file_count:1,frame_cut_available:true,files:[{index:0,number:1,name:'r.ts',size:'1 MB',bytes:1,kinks:[]}],parts:[{url:'data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw==',tiles:[0,1,2,3,4].map(i=>({file_index:0,number:1,frame_index:i,chunk_seconds:i*10,interval_end_seconds:(i+1)*10,left:i*20,top:0,width:20,height:100}))}]},recu:{status:'disabled',segments:{}},prefetch:{},mosaic_status:{state:'ready'},action_queue:{},library_background:{},non_nsfw_background:{},background_scheduler:{}};
      state.frameCutMode=true; state.decisionMode='Leave for review'; state.frameDecisions={}; state.lastFrameSelectionIndex=null; state.frameTilesSignature=''; renderMosaics(); renderFiles(); renderReviewGranularity();
    }""")
    page.locator('#mosaic-list .tile-hit[data-frame-index="1"]').click()
    page.keyboard.down('Shift'); page.locator('#mosaic-list .tile-hit[data-frame-index="4"]').click(); page.keyboard.up('Shift')
    assert page.evaluate('Object.keys(state.frameDecisions).map(Number).sort((a,b)=>a-b)') == [1,2,3,4]
    page.locator('#mosaic-list .tile-hit[data-frame-index="4"]').click()
    page.keyboard.down('Shift'); page.locator('#mosaic-list .tile-hit[data-frame-index="2"]').click(); page.keyboard.up('Shift')
    assert page.evaluate('Object.keys(state.frameDecisions).map(Number).sort((a,b)=>a-b)') == [1]
    page.locator('#clear-review-selections').click()
    assert page.evaluate('Object.keys(state.frameDecisions)') == []

    # Load Rapid and verify a new chunk reconstructs *only* its own draft and
    # clears anchors/frame selections while preserving the chosen Frame Cut mode.
    page.add_script_tag(content=RAPID)
    result = page.evaluate("""() => {
      state.frameCutMode=true; state.frameDecisions={'99':'Cumshots'}; state.lastFrameSelectionIndex=99; state.lastFileSelectionIndex=3;
      initializeSelectionsFromCurrent({mode:'review',draft:{},chunk:{files:[{index:0}]}});
      return {frameDecisions:state.frameDecisions,lastFrame:state.lastFrameSelectionIndex,lastFile:state.lastFileSelectionIndex,frameCutMode:state.frameCutMode};
    }""")
    assert result == {'frameDecisions':{},'lastFrame':None,'lastFile':None,'frameCutMode':True}, result
    browser.close()

print('PASS: v2.15.7 stabilization — draft isolation, symmetric Shift ranges, clear/reset, exact preview duration')
