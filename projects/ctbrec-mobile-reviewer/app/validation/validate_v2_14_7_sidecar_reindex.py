#!/usr/bin/env python3
"""v2.14.7 regression: stale ready-index self-heals from valid on-disk mosaics."""
from __future__ import annotations
import importlib.util, json, sys, tempfile, threading, types, shutil
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module

server = load('server_v2147_reindex', ROOT/'ctbrec_mobile_server.py')
mosaic = load('mosaic_v2147_reindex', ROOT/'ctbrec_mosaic_sort_lite.py')

tmp = Path(tempfile.mkdtemp(prefix='v2147-sidecar-reindex-'))
try:
    model = tmp/'ddawgtylol'; model.mkdir()
    out = model/mosaic.MOSAIC_DIRNAME; out.mkdir()
    starts = [
        datetime(2026,1,31,4,47,51),
        datetime(2026,2,1,3,5,9),
        datetime(2026,2,1,3,51,55),
    ]
    keys=[]
    for i, stamp in enumerate(starts, start=1):
        src = model/f'ddawgtylol_{stamp:%Y-%m-%d_%H-%M-%S}_tail_15m00s_est.ts'
        src.write_bytes((f'video-{i}'*100).encode())
        st=src.stat()
        key=f'{stamp.isoformat()}|{stamp.isoformat()}|n=1'
        keys.append(key)
        sig=f'sig-{i}'
        jpg=out/f'chunk_{i:04d}_{stamp:%Y%m%d_%H%M%S}_MASSIVE.jpg'
        jpg.write_bytes(b'fake-jpg')
        side={
            'signature':sig,'chunk_key':key,'folder':str(model),'outputs':[str(jpg)],
            'files':[{'path':str(src),'size':st.st_size,'mtime':st.st_mtime}],
            'generated_at':'2026-09-15T00:00:00',
            'mobile_layout_v2':{'version':2,'mode':'original','signature':sig,'parts':[{'output':str(jpg),'width':1,'height':1,'tiles':[{'file_index':0,'left_px':0,'top_px':0,'width_px':1,'height_px':1}]}]},
        }
        (out/f'chunk_{i:04d}_{stamp:%Y%m%d_%H%M%S}_MASSIVE.sources.json').write_text(json.dumps(side),encoding='utf-8')
    # One deliberately reviewed sidecar must NOT be resurrected.
    (model/mosaic.STATE_FILENAME).write_text(json.dumps({'reviewed':{keys[1]:'done'}}),encoding='utf-8')
    # One orphan legacy JPEG must not count as sortable work.
    (out/'chunk_0099_20260101_000000_MASSIVE.jpg').write_bytes(b'orphan')

    state = object.__new__(server.MobileReviewerState)
    state.mosaic = mosaic
    state.mosaic_settings = {'extensions':'mp4,ts','queue_order':'Largest chunks first'}
    state.mosaic_duration_cache = mosaic.DurationCache(tmp/'durations.json')
    state.mosaic_manifest = mosaic.MosaicManifest(tmp/'manifest.json')
    state.review = types.SimpleNamespace(MOSAIC_DIRNAME='._review_chunk_mosaics')
    state.review_settings = {'extensions':'mp4,ts'}
    state.lock = threading.RLock()
    state.ready_index = {'snapshots':{}}
    state.ready_count_cache = {}
    state.catalog_view_cache = {}
    state.offline_media_lookup = {}
    state.validated_layout_cache = {}
    state.queues = {}
    state.action_queue = {'jobs':[]}
    state.interactive_demand = threading.Event()
    state.config = {'speed_mode':{'open_fast_initial_chunks':3},'recu':{'enabled':False}}
    state.sort_session_active_until = 0.0
    state.active_sort_queue_id = ''
    state.model_folders = types.MethodType(lambda self, mode, name, drives: [model], state)
    state._chunk_is_claimed = types.MethodType(lambda self, chunk: False, state)
    state._pending_chunk_signatures = types.MethodType(lambda self: set(), state)
    state._embedded_layout_entry = types.MethodType(lambda self, mode, chunk, outputs: {'parts':[{}]} if outputs else None, state)
    state._layout_cache_entry = types.MethodType(lambda self, mode, chunk, outputs: None, state)
    state.current_payload = types.MethodType(lambda self, qid: {'model':self.queues[qid]['model'],'chunk':{'signature':self.queues[qid]['chunks'][0].signature}}, state)
    state.start_recu_for_queue = types.MethodType(lambda self, qid, force=False: None, state)
    state._save_ready_index = types.MethodType(lambda self: None, state)

    # Seed the derived ready-index with only ONE chunk, reproducing the field bug.
    first_src=next(model.glob('ddawgtylol_2026-01-31*.ts')); st=first_src.stat(); stamp=starts[0]
    first_jpg=out/f'chunk_0001_{stamp:%Y%m%d_%H%M%S}_MASSIVE.jpg'
    seed_row={
        'signature':'sig-1','start':stamp.isoformat(),'end':stamp.isoformat(),'source_bytes':st.st_size,
        'folder':str(model),'model_folder':str(model),'files':[{'path':str(first_src),'start':stamp.isoformat(),'size':st.st_size,'mtime':st.st_mtime,'duration':900,'duration_source':'tail-tag','duration_warning':''}],
        'source_identity':[], 'mosaics':[str(first_jpg)], 'ready':True, 'idx':1,'key':keys[0],'durations_prepared':False,'preparation_warning':''
    }
    key=state._ready_key('original','ddawgtylol')
    state.ready_index['snapshots'][key]={'mode':'original','model':'ddawgtylol','chunks':[seed_row],'ready_chunks':1,'updated_at':'2026-09-15T00:00:00'}

    # Disk has 3 sidecars, but one is reviewed. Reindex should yield 2 sortable chunks,
    # not the stale 1 and not all 3. The orphan JPEG must be ignored.
    assert state._existing_mosaic_sidecar_count('original','ddawgtylol',set()) == 2
    result = state.quick_load_queue('original','ddawgtylol',set())
    assert result['ready'] is True, result
    assert result['ready_chunks'] == 2, result
    assert result['recovered_existing_mosaics'] == 1, result
    assert result['source'] == 'sidecar-reindex', result
    q=state.queues[result['queue_id']]
    signatures={c.signature for c in q['chunks']}
    assert signatures == {'sig-1','sig-3'}, signatures
    assert 'sig-2' not in signatures

    # Manual Ahead must reindex before deciding there are no upcoming chunks.
    # Monkeypatch task creation to execute synchronously for this regression.
    state.create_task = types.MethodType(lambda self, name, worker, pool=None: worker(lambda *args: None), state)
    state.ensure_chunk_mosaic = types.MethodType(lambda self, queue_data, chunk, force, progress: None, state)
    ahead = state.prefetch_task(result['queue_id'])
    assert ahead['target'] >= 1, ahead
    assert '0/0 upcoming' not in str(ahead.get('message','')), ahead

    # Static release markers.
    server_text=(ROOT/'ctbrec_mobile_server.py').read_text(encoding='utf-8')
    app=(ROOT/'static/app.js').read_text(encoding='utf-8')
    sw=(ROOT/'static/service-worker.js').read_text(encoding='utf-8')
    html=(ROOT/'static/index.html').read_text(encoding='utf-8')
    assert 'CTBRecMobile/2.15.10' in server_text
    assert 'sidecar-reindex' in server_text and 'recovered_existing_mosaics' in server_text
    assert 'Reindexed ${recovered}' in app
    assert 'ctbrec-shell-v21510' in sw and 'app.js?v=21510' in html
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print('PASS v2.14.7 stale ready-index / sidecar reindex / Ahead recovery regression suite')
