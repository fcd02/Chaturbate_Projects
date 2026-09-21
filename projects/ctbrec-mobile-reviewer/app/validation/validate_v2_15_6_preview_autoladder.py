#!/usr/bin/env python3
from pathlib import Path
from playwright.sync_api import sync_playwright

ROOT=Path(__file__).resolve().parents[1]
APP=(ROOT/'static/app.js').read_text(encoding='utf-8')
HTML=(ROOT/'static/index.html').read_text(encoding='utf-8')
SW=(ROOT/'static/service-worker.js').read_text(encoding='utf-8')
SERVER=(ROOT/'ctbrec_mobile_server.py').read_text(encoding='utf-8')

assert 'CTBRecMobile/2.15.10' in SERVER
assert 'ctbrec-shell-v21510' in SW
assert 'app.js?v=21510' in HTML and 'rapid.js?v=21510' in HTML

# UX: fallback timeline is integrated into the player instead of a separate Fast seek box.
assert 'preview-player-shell' in HTML
assert 'preview-source-controls' in HTML
assert 'preview-source-timeline' in HTML
assert 'preview-server-seek' not in HTML
assert 'Force compatibility now' in HTML
assert 'previewUsesSourceTimeline' in APP
assert 'seekCurrentPreviewToAbsolute' in APP
assert "video.controls = !enabled" in APP
assert "source_timeline_seek" in APP

# Automatic ladder and no false multi-file advance on a short/dead pipe.
assert "seekable zero-reencode MP4 wrapper" in APP
assert "trying compatibility playback automatically" in APP
assert "terminal_playback_failure" in APP
assert "premature_stream_end" in APP
assert "previewFailover('premature-ended')" in APP
assert "if (state.previewIndex + 1 < state.previewItems.length) playPreviewItem(state.previewIndex + 1, false);" in APP
assert "This file stays selected here instead of skipping to the next recording" in APP

# Source timeline helper is unit-testable in a real browser even though the backend stream restarts at zero.
html=HTML
for tag in ('<script src="/static/app.js?v=21510"></script>','<script src="/static/rapid.js?v=21510"></script>','<link rel="stylesheet" href="/static/styles.css?v=21510">','<link rel="manifest" href="/static/manifest.webmanifest">'):
    html=html.replace(tag,'')
mock=r'''
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
    result=page.evaluate('''() => {
      const video=document.getElementById('preview-video');
      video.dataset.previewStage='compatibility';
      video.dataset.streamOffset='120';
      const item={duration:600};
      updatePreviewSourceControls(item, video, 120);
      const slider=document.getElementById('preview-source-timeline');
      const controls=document.getElementById('preview-source-controls');
      const early=previewReachedSourceEnd(item,{dataset:{previewStage:'remux',streamOffset:'120'},currentTime:5},120);
      const end=previewReachedSourceEnd(item,{dataset:{previewStage:'compatibility',streamOffset:'590'},currentTime:9},590);
      return {
        slider:Number(slider.value), max:Number(slider.max), hidden:controls.classList.contains('hidden'), nativeControls:video.controls,
        earlyFinished:early.finished, earlyAbsolute:early.absolute, endFinished:end.finished, endAbsolute:end.absolute
      };
    }''')
    assert result['slider'] == 120, result
    assert result['max'] == 600 and result['hidden'] is False and result['nativeControls'] is False, result
    assert result['earlyFinished'] is False and result['earlyAbsolute'] == 125, result
    assert result['endFinished'] is True and result['endAbsolute'] == 599, result
    browser.close()

print('PASS: v2.15.6 source-timeline fallback, automatic playback ladder, and premature-end guard')
