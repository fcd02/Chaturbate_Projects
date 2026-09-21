#!/usr/bin/env python3
from pathlib import Path
import hashlib
import subprocess
import tempfile
import threading
import time
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import ctbrec_mobile_server as server

APP = (ROOT / 'static' / 'app.js').read_text(encoding='utf-8')
CSS = (ROOT / 'static' / 'styles.css').read_text(encoding='utf-8')
HTML = (ROOT / 'static' / 'index.html').read_text(encoding='utf-8')
SW = (ROOT / 'static' / 'service-worker.js').read_text(encoding='utf-8')
SERVER = (ROOT / 'ctbrec_mobile_server.py').read_text(encoding='utf-8')

assert 'CTBRecMobile/2.15.10' in SERVER
assert 'ctbrec-shell-v21510' in SW
assert 'app.js?v=21510' in HTML and 'styles.css?v=21510' in HTML

# The new path must be a materialized, Range-seekable MP4 cache, not another
# non-seekable live FFmpeg pipe.
for token in [
    'PREVIEW_SEEKABLE_CACHE_DIR',
    'def preview_seekable_copy',
    '"seekable_url"',
    'seekable_cache_build_start',
    'seekable_cache_build_complete',
    'seekable_cache_hit',
    'output_duration < source_duration * 0.90',
    'seekable_cache_max_gb',
    'self.send_range_file(cached, diag_id=diag_id)',
]:
    assert token in SERVER, token

# Chromium ladder: native MP4 remains direct; non-native TS starts with cached
# seekable wrapper; failures fall to compatibility without advancing files.
assert "Boolean(item.local_disk_url) && Boolean(item.direct)" in APP
assert "preferSeekable" in APP
assert "stage === 'seekable'" in APP
assert "restartAt(absolute, 'seekable')" in APP
assert "currentStage === 'seekable' && item.compatibility_url" in APP
assert "previewFailover('premature-ended')" in APP

# Fixed geometry contract.
for token in [
    'height: clamp(220px, 52vh, 440px)',
    'height: 100%',
    'object-fit: contain',
    '#preview-note { min-height:',
    '#preview-compatibility { min-width:',
]:
    assert token in CSS, token
assert "compatibilityButton.classList.remove('hidden')" in APP

ffmpeg = Path('/usr/bin/ffmpeg')
ffprobe = Path('/usr/bin/ffprobe')
if ffmpeg.is_file() and ffprobe.is_file():
    class Review:
        def resolve_ffmpeg(self, _settings):
            return ffmpeg, ffprobe

    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        source = td / 'source.ts'
        cache = td / 'cache'
        cmd = [
            str(ffmpeg), '-hide_banner', '-loglevel', 'error', '-nostdin',
            '-f', 'lavfi', '-i', 'testsrc=size=320x180:rate=30',
            '-f', 'lavfi', '-i', 'sine=frequency=880:sample_rate=48000',
            '-t', '4.0', '-c:v', 'libx264', '-preset', 'ultrafast',
            '-pix_fmt', 'yuv420p', '-g', '30', '-c:a', 'aac', '-b:a', '96k',
            '-f', 'mpegts', '-y', str(source),
        ]
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)

        old_cache = server.PREVIEW_SEEKABLE_CACHE_DIR
        server.PREVIEW_SEEKABLE_CACHE_DIR = cache
        try:
            state = object.__new__(server.MobileReviewerState)
            state.lock = threading.RLock()
            state.config = {'video_preview': {'cache_hours': 18, 'seekable_cache_max_gb': 1}}
            state.review_settings = {}
            state.review = Review()
            state.preview_duration_cache = {}
            state.preview_seekable_locks = {}
            state.preview_diagnostics = {'diag': {'events': []}}
            state.preview_diagnostics_order = ['diag']

            out1 = server.MobileReviewerState.preview_seekable_copy(state, source, 'diag')
            assert out1.is_file() and out1.suffix == '.mp4' and out1.stat().st_size > 64 * 1024
            duration = server.MobileReviewerState.preview_exact_duration(state, out1)
            assert duration >= 3.5, duration
            digest1 = hashlib.sha256(out1.read_bytes()).hexdigest()
            out2 = server.MobileReviewerState.preview_seekable_copy(state, source, 'diag')
            digest2 = hashlib.sha256(out2.read_bytes()).hexdigest()
            assert out1 == out2 and digest1 == digest2
            events = [row.get('event') for row in state.preview_diagnostics['diag']['events']]
            assert 'seekable_cache_build_complete' in events and 'seekable_cache_hit' in events, events

            # Simulate an ffmpeg command that returns success but creates a tiny-
            # duration wrapper. The safety check must reject it rather than serve
            # the same 0.19-second failure observed in field diagnostics.
            badsource = td / 'bad.ts'
            badsource.write_bytes(b'not-real-media' * 10000)
            state.preview_duration_cache = {}
            original_run = server.subprocess.run
            original_duration = state.preview_exact_duration
            def fake_run(command, *args, **kwargs):
                Path(command[-1]).write_bytes(b'X' * 70000)
                class Result:
                    returncode = 0
                    stderr = ''
                return Result()
            state.preview_exact_duration = lambda p, *a, **k: 100.0 if Path(p) == badsource else 0.2
            server.subprocess.run = fake_run
            try:
                try:
                    server.MobileReviewerState.preview_seekable_copy(state, badsource, 'diag')
                    raise AssertionError('truncated remux was accepted')
                except RuntimeError as exc:
                    assert 'truncated preview' in str(exc)
            finally:
                server.subprocess.run = original_run
                state.preview_exact_duration = original_duration
        finally:
            server.PREVIEW_SEEKABLE_CACHE_DIR = old_cache

# Browser-level stage choice and fixed viewport test.
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
    return response({ok:true});
  };
  try { Object.defineProperty(navigator,'serviceWorker',{value:undefined,configurable:true}); } catch(_) {}
})();
'''
with sync_playwright() as pw:
    browser = pw.chromium.launch(headless=True, executable_path='/usr/bin/chromium', args=['--no-sandbox'])
    page = browser.new_page(viewport={'width':1100,'height':900})
    page.set_content(html, wait_until='domcontentloaded')
    page.add_style_tag(content=CSS)
    page.add_script_tag(content=mock)
    page.add_script_tag(content=APP)
    result = page.evaluate('''() => {
      const modal=document.getElementById('preview-modal'); modal.classList.remove('hidden');
      const video=document.getElementById('preview-video');
      state.previewItems=[{number:1,name:'raw.ts',size:'1 MB',duration:600,direct:false,local_disk_url:'http://mock/raw',seekable_url:'http://mock/seekable',compatibility_url:'http://mock/compat',remux_url:'http://mock/remux',stream_url_base:'http://mock/base'}];
      playPreviewItem(0,false,{autoplay:false});
      const stageTs=video.dataset.previewStage;
      const shell=document.querySelector('.preview-player-shell');
      const controls=document.querySelector('.preview-controls');
      const before={h:shell.getBoundingClientRect().height, top:controls.getBoundingClientRect().top};
      video.dataset.previewStage='compatibility';
      updatePreviewSourceControls(state.previewItems[0],video,120);
      document.getElementById('preview-note').textContent='Compatibility playback is active automatically. A deliberately longer note must not move the navigation controls because the note area is reserved.';
      const after={h:shell.getBoundingClientRect().height, top:controls.getBoundingClientRect().top};
      state.previewItems=[{number:1,name:'native.mp4',size:'1 MB',duration:600,direct:true,local_disk_url:'http://mock/native',seekable_url:'http://mock/seekable',compatibility_url:'http://mock/compat',remux_url:'http://mock/remux',stream_url_base:'http://mock/base'}];
      playPreviewItem(0,false,{autoplay:false});
      const stageMp4=video.dataset.previewStage;
      return {stageTs,stageMp4,before,after,compatHidden:document.getElementById('preview-compatibility').classList.contains('hidden')};
    }''')
    assert result['stageTs'] == 'seekable', result
    assert result['stageMp4'] == 'direct', result
    assert abs(result['before']['h'] - result['after']['h']) < 0.5, result
    assert abs(result['before']['top'] - result['after']['top']) < 0.5, result
    assert result['compatHidden'] is False, result
    browser.close()

print('PASS: v2.15.10 seekable zero-reencode cache, optimized preview ladder, truncated-remux rejection, and fixed preview geometry')
