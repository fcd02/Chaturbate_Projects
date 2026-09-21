#!/usr/bin/env python3
"""v2.14.8 regressions: requested-model stickiness, bounded queueing, leave-stop, preview fallbacks."""
from __future__ import annotations
import importlib.util, sys, tempfile, threading, types, shutil
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module

server = load('server_v2148_queue_preview', ROOT/'ctbrec_mobile_server.py')

# Stable queue reconciliation: queue size is installed once, current stays first,
# already-ready mosaics stay ahead of missing chunks, then the queue only shrinks.
state = object.__new__(server.MobileReviewerState)
state.lock = threading.RLock()
state.queues = {}
state.action_queue = {'jobs': []}
state.priority_generation_targets = {}
state.priority_generation_tasks = {}
state.tasks = {}
state.prefetch_futures = {}
state.priority_model_key = ''
state.action_wakeup = threading.Event()
state.active_sort_queue_id = ''
state.sort_session_active_until = 0.0
state._pending_chunk_signatures = types.MethodType(lambda self: set(), state)
state._chunk_is_claimed = types.MethodType(lambda self, chunk: False, state)

def chunk(i: int, ready: bool):
    video = types.SimpleNamespace(path=Path(f'/tmp/video-{i}.ts'))
    return types.SimpleNamespace(
        signature=f'sig-{i}', files=[video], mosaics=[Path(f'/tmp/m-{i}.jpg')] if ready else [],
        source_bytes=(1000-i)*1000, start=datetime(2026,1,1)+timedelta(hours=i),
    )

current = chunk(2, True)
state.queues['q'] = {
    'id':'q','mode':'original','model':'alpha','drives':['E:'],'chunks':[current],
    'history':[],'drafts':{},'initial_count':1,'known_total_chunks':1,
    'prefetch_status':{},'abandoned':False,
}
all_chunks = [chunk(1, False), chunk(2, True), chunk(3, True), chunk(4, False), chunk(5, True)]
state._reconcile_model_chunks_into_queue('q', all_chunks)
q = state.queues['q']
assert q['chunks'][0].signature == 'sig-2'
assert len(q['chunks']) == 5
assert [c.signature for c in q['chunks'][1:3]] == ['sig-3','sig-5'], [c.signature for c in q['chunks']]
assert q['known_total_chunks'] == 5

# Leaving the model marks the queue abandoned and cancels both priority/manual generation.
priority = types.SimpleNamespace(status='running', cancel_requested=False, progress='', updated_at=0.0)
manual = types.SimpleNamespace(status='queued', cancel_requested=False, progress='', updated_at=0.0)
state.tasks = {'p': priority, 'm': manual}
state.priority_generation_tasks = {'original:alpha:E:':'p'}
state.priority_generation_targets = {'original:alpha:E:': {'q'}}
state.queues['q']['manual_prefetch_task_id'] = 'm'
state.active_sort_queue_id = 'q'
state.sort_session_active_until = 99999999999
state.mark_sort_session_inactive()
assert state.queues['q']['abandoned'] is True
assert priority.cancel_requested is True and manual.cancel_requested is True
assert state.active_sort_queue_id == ''

# A no-ready tile click prepares only ONE first mosaic, not the whole model.
state2 = object.__new__(server.MobileReviewerState)
state2.lock = threading.RLock(); state2.interactive_demand = threading.Event(); state2.action_wakeup = threading.Event(); state2.stop_event = threading.Event()
state2.priority_generation_targets = {}; state2.priority_generation_tasks = {}; state2.priority_model_key=''; state2.priority_model_epoch=0; state2.tasks={}; state2.queues={}
state2.model_folders = types.MethodType(lambda self, mode, model, drives: [Path('/tmp')], state2)
chunks = [chunk(i, False) for i in range(1,7)]
state2._build_chunks_for_library_model = types.MethodType(lambda self, mode, model, folders, progress: chunks, state2)
state2._pending_chunk_signatures = types.MethodType(lambda self: set(), state2)
state2._chunk_is_claimed = types.MethodType(lambda self, c: False, state2)
state2._persist_ready_snapshot = types.MethodType(lambda self, *a, **k: None, state2)
state2.background_settings_for_mode = types.MethodType(lambda self, mode: {'upcoming_count': 3}, state2)
state2._chunk_mosaic_ready = types.MethodType(lambda self, qd, c: bool(c.mosaics), state2)
ensure_calls=[]
def ensure(self, qd, c, force, progress):
    ensure_calls.append(c.signature); c.mosaics=[Path(f'/tmp/{c.signature}.jpg')]; progress('done',1,1)
state2.ensure_chunk_mosaic = types.MethodType(ensure, state2)
def create_sync(self, label, worker, pool=None):
    worker(lambda *args, **kwargs: None)
    return 'task-sync'
state2.create_task = types.MethodType(create_sync, state2)
state2.priority_executor = object()
state2.prioritize_model_mosaics('original','alpha',{'E:'},queue_id='')
assert ensure_calls == ['sig-1'], ensure_calls

# Real ffmpeg preview integration: remux and compatibility transcode must both
# emit playable-looking fragmented MP4 bytes from a synthetic TS source.
import io, subprocess, shutil
ffmpeg = shutil.which('ffmpeg')
ffprobe = shutil.which('ffprobe')
if ffmpeg and ffprobe:
    temp = Path(tempfile.mkdtemp(prefix='v2148-preview-'))
    try:
        src = temp/'sample.ts'
        subprocess.run([
            ffmpeg, '-hide_banner', '-loglevel', 'error', '-f', 'lavfi', '-i', 'testsrc=size=320x180:rate=15',
            '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=44100', '-t', '1.5',
            '-c:v', 'libx264', '-preset', 'ultrafast', '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-f', 'mpegts', '-y', str(src)
        ], check=True)
        server.STATE = types.SimpleNamespace(
            review=types.SimpleNamespace(resolve_ffmpeg=lambda settings: (Path(ffmpeg), Path(ffprobe))),
            review_settings={},
        )
        def exercise(transcode: bool):
            h = object.__new__(server.Handler)
            h.wfile = io.BytesIO(); h._status = None; h._headers = {}
            h.send_response = lambda status: setattr(h, '_status', status)
            h.send_header = lambda key, value: h._headers.__setitem__(key, value)
            h.end_headers = lambda: None
            h.send_json = lambda payload, status=200, headers=None: setattr(h, '_status', status)
            h.send_preview_stream(src, 0.0, transcode=transcode)
            raw = h.wfile.getvalue()
            assert h._status == 200, h._status
            assert len(raw) > 1024, len(raw)
            assert b'ftyp' in raw[:256] or b'moof' in raw[:1024], raw[:64]
        exercise(False)
        exercise(True)
    finally:
        shutil.rmtree(temp, ignore_errors=True)

# Static release behavior: no automatic suggestion hop and robust preview chain.
app=(ROOT/'static/app.js').read_text(encoding='utf-8')
rapid=(ROOT/'static/rapid.js').read_text(encoding='utf-8')
server_text=(ROOT/'ctbrec_mobile_server.py').read_text(encoding='utf-8')
assert 'suggestion?.name' not in app[app.index('async function openModel'):app.index('async function nextReadyModel')]
assert 'Generating its first mosaic now' in app
assert 'suggestion?.name' not in rapid and 'jumping to' not in rapid
assert 'Generating its first mosaic now' in rapid
assert 'previewCandidates(item, compatibility' in app
assert "kind: 'disk'" in app and "kind: 'remux'" in app and "kind: 'compatibility'" in app
assert "kind: 'direct'" in app and "kind: 'hls'" in app
assert 'candidate.preseeked' in app, 'remux/transcode routes must not double-seek to the Recu offset'
assert 'Preview remux' in server_text or "Preview {'transcode' if transcode else 'remux'}" in server_text
assert 'frag_keyframe+empty_moov+default_base_moof+omit_tfhd_offset' in server_text

print('PASS v2.14.8 requested-model / bounded-queue / leave-stop / preview-fallback regression suite')
