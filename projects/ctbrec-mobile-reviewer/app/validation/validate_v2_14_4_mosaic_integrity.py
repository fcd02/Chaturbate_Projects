#!/usr/bin/env python3
"""v2.14.4 regression: durable traversal, fresh size refresh, mosaic integrity, end-relative names."""
from __future__ import annotations
import hashlib, importlib.util, json, os, shutil, subprocess, sys, tempfile, threading
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

server = load('ctbrec_server_v2144_test', ROOT/'ctbrec_mobile_server.py')
mosaic = load('ctbrec_mosaic_v2144_test', ROOT/'ctbrec_mosaic_sort_lite.py')
review = load('ctbrec_review_v2144_test', ROOT/'ctbrec_review_folder_sort_lite.py')

# 1) Traversal identity must be immune to mutable sizes/drive metadata.
state = object.__new__(server.MobileReviewerState)
plan_a = [
    {'mode':'original','name':'alpha','bytes':100,'drives':['E:']},
    {'mode':'review','name':'beta','bytes':50,'drives':['F:']},
]
plan_b = [
    {'mode':'original','name':'alpha','bytes':999999,'drives':['G:']},
    {'mode':'review','name':'beta','bytes':1,'drives':['C:','E:']},
]
assert state._library_plan_fingerprint(plan_a) == state._library_plan_fingerprint(plan_b)
assert state._library_plan_fingerprint(plan_a) != state._library_plan_fingerprint(list(reversed(plan_a)))

# 2) Persisted plan round trip is durable and carries ordering.
tmp = Path(tempfile.mkdtemp(prefix='v2144-plan-'))
old_plan_path = server.LIBRARY_PLAN_PATH
server.LIBRARY_PLAN_PATH = tmp/'plan.json'
try:
    fp = state._library_plan_fingerprint(plan_a)
    state._persist_library_plan(plan_a, fp)
    saved = state._load_persisted_library_plan()
    assert saved and saved['fingerprint'] == fp
    assert [(r['mode'], r['name']) for r in saved['plan']] == [('original','alpha'),('review','beta')]
finally:
    server.LIBRARY_PLAN_PATH = old_plan_path
    shutil.rmtree(tmp, ignore_errors=True)

# 3) Operational settings no longer expose arbitrary HTML maximums.
html = (ROOT/'static'/'index.html').read_text(encoding='utf-8')
for field in [
    'library-idle-minutes','library-rescan-minutes','frame-cut-idle-minutes',
    'keep-last-gap','keep-last-grace','nsfw-idle-minutes','nsfw-rescan-minutes',
    'nsfw-spacing','nsfw-batch-size','nsfw-columns','nsfw-tile-width','nsfw-max-tiles',
    'original-bg-count','original-bg-idle-minutes','review-bg-count','review-bg-idle-minutes',
    'original-spacing','original-columns','original-tile-width','original-max-frames',
    'review-spacing','review-columns','review-tile-width','review-max-frames',
    'speed-ready-suggestions','speed-offline-chunks','speed-offline-max-mb','offline-pack-limit','offline-pack-max-mb'
]:
    marker = f'id="{field}"'
    pos = html.index(marker)
    tag_start = html.rfind('<input', 0, pos)
    tag_end = html.index('>', pos)
    tag = html[tag_start:tag_end+1]
    assert ' max=' not in tag, f'arbitrary max remains on {field}: {tag}'

# 4) Client refreshes visible model tiles after any catalog scan, not deletion only.
js = (ROOT/'static'/'app.js').read_text(encoding='utf-8')
assert 'lastCatalogUpdatedAt' in js
assert 'loadModels({ force: true, resetWindow: false })' in js
assert 'rebuild-chunk' in js and 'rebuild-chunk-button' in js

# 5) Server has the reconciliation endpoint and strict v3 layout validation.
server_text = (ROOT/'ctbrec_mobile_server.py').read_text(encoding='utf-8')
assert '/rebuild-chunk' in server_text
assert '_purge_mosaic_artifacts_for_chunk_sources' in server_text
assert 'expected_seconds' in server_text and 'actual_seconds' in server_text
assert 'mobile_library_mosaic_plan.json' in server_text
assert 'server_version = "CTBRecMobile/2.15.10"' in server_text

# 6) End-relative naming semantics: final 15m of a 2h chunk => 15m -> 0m.
dummy_root = Path(tempfile.mkdtemp(prefix="v2144-name-"))
try:
    dummy_model = dummy_root / "example_model"; dummy_model.mkdir()
    dummy_review = dummy_model / "Review"; dummy_review.mkdir()
    dummy_path = dummy_review / "source.mp4"; dummy_path.write_bytes(b"x")
    dummy_video = review.VideoInfo(dummy_path, datetime(2026,1,1,12,0,0), 1, dummy_path.stat().st_mtime, 7200.0)
    dummy_chunk = review.Chunk(dummy_review, dummy_model, [dummy_video], datetime(2026,1,1,12,0,0), datetime(2026,1,1,14,0,0), 1, "dummy")
    name = review.build_trim_output_name(dummy_chunk, 6300.0, 7200.0)
finally:
    shutil.rmtree(dummy_root, ignore_errors=True)
assert '_endminus_15m00s_to_0m00s' in name, name

# 7) Real ffmpeg/Pillow original mosaic: sidecar/tile counts must exactly match and
# forced regeneration with a different part size must remove stale old parts.
ffmpeg = shutil.which('ffmpeg')
if ffmpeg:
    tmp = Path(tempfile.mkdtemp(prefix='v2144-mosaic-'))
    try:
        model_dir = tmp/'model'; model_dir.mkdir()
        start = datetime(2026,1,1,12,0,0)
        videos=[]
        for i, color in enumerate(('red','blue')):
            path = model_dir/f'model_20260101_{120000+i*10:06d}_{i}.mp4'
            subprocess.run([
                ffmpeg,'-hide_banner','-loglevel','error','-f','lavfi','-i',f'color=c={color}:s=320x180:r=5',
                '-t','8','-c:v','libx264','-pix_fmt','yuv420p','-y',str(path)
            ], check=True, timeout=30)
            st=path.stat()
            videos.append(mosaic.VideoInfo(path,start+timedelta(seconds=i*10),st.st_size,st.st_mtime,8))
        chunk=mosaic.Chunk(1,model_dir,videos,start,start+timedelta(seconds=18),sum(v.size for v in videos),key='k',signature='sig-v2144')
        cache=mosaic.DurationCache(tmp/'durations.json')
        manifest=mosaic.MosaicManifest(tmp/'manifest.json')
        cancel=threading.Event(); holder=[None]
        settings={
            'ffmpeg_path':ffmpeg,'use_existing_mosaics':True,'sample_every_seconds':30,
            'max_total_frames':4,'columns':1,'tile_width':180,'max_tiles_per_image':1,
            'frame_extract_batch_size':6,'frame_extract_timeout_seconds':20,
        }
        ok,msg,outputs=mosaic.generate_mosaic(chunk,settings,cache,manifest,cancel,holder,lambda *a:None,force_regenerate=True)
        assert ok, msg
        assert len(outputs)==2, outputs
        sidecar=next((model_dir/mosaic.MOSAIC_DIRNAME).glob('*.sources.json'))
        raw=json.loads(sidecar.read_text(encoding='utf-8'))
        assert raw['mobile_layout_v2']['version'] >= 3
        flat=[t for p in raw['mobile_layout_v2']['parts'] for t in p['tiles']]
        assert len(flat)==2
        assert [t['frame_index'] for t in flat]==[0,1]
        assert all('local_seconds' in t for t in flat)
        assert all('duration' in row for row in raw['files'])
        old_parts=set((model_dir/mosaic.MOSAIC_DIRNAME).glob('*_part*of*.jpg'))
        assert old_parts
        settings['max_tiles_per_image']=20
        ok,msg,outputs2=mosaic.generate_mosaic(chunk,settings,cache,manifest,cancel,holder,lambda *a:None,force_regenerate=True)
        assert ok, msg
        assert len(outputs2)==1
        assert all(not p.exists() for p in old_parts), 'stale old part JPEG remained'
        raw2=json.loads(sidecar.read_text(encoding='utf-8'))
        assert len(raw2['mobile_layout_v2']['parts'])==1
        assert len(raw2['mobile_layout_v2']['parts'][0]['tiles'])==2
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
else:
    print('SKIP real mosaic media test: ffmpeg unavailable')

print('PASS v2.14.4 traversal/settings/size-refresh/mosaic-integrity/end-relative regression suite')
