"""Field-regression tests for v2.13.3.

Covers the exact failures seen on the user's real v2.13.x installation:
settings-form overwrite, ready-count collapse on open, ready snapshot erasure,
Recu disabled by corrupted config, EZ Sort disappearance, and the batched
Original-mosaic `normalized` NameError.
"""
from __future__ import annotations
import copy, json, shutil, subprocess, sys, tempfile, threading, time
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import ctbrec_mobile_server as server
import ctbrec_mobile_model_admin as admin_mod
import ctbrec_mosaic_sort_lite as mosaic

# 1) Exact settings corruption fingerprint and conservative repair.
corrupt = {
    'mosaic_settings': {'sample_every_seconds':15,'columns':1,'tile_width':160,'max_total_frames':1,'use_existing_mosaics':False,'recu_enabled':True},
    'review_settings': {'sample_every_seconds':15,'columns':1,'tile_width':160,'max_total_frames':1,'use_existing_mosaics':False},
    'recu': {'enabled':False,'browser_debug_port':1024,'chrome_path':'','base_url':'https://recu.me','auto_refresh_current':True},
    'background_mosaics': {'original': {'enabled':False,'upcoming_count':0,'idle_only':False,'idle_minutes':0}, 'review': {'enabled':False,'upcoming_count':0,'idle_only':False,'idle_minutes':0}},
    'library_background': {'enabled':False,'original_enabled':False,'review_enabled':False,'idle_minutes':0,'rescan_minutes':15,'pause_when_active':False},
    'non_nsfw_cleanup': {'enabled':False,'sample_every_seconds':30,'columns':1,'tile_width':160,'batch_size':1},
    'model_admin': {'enabled':False,'auto_discover':False,'models_json_paths':[]},
    'live_bridge': {'enabled':False,'auto_discover':False,'bridge_port':8791},
    'action_queue': {'frame_cut_idle_minutes':0},
}
assert server._looks_like_v213_settings_form_corruption(corrupt)
repaired = copy.deepcopy(corrupt)
# merge nested defaults the same way state init does before applying repair
for key in ('recu','background_mosaics','library_background','non_nsfw_cleanup','action_queue','model_admin','live_bridge'):
    base = copy.deepcopy(server.DEFAULT_CONFIG[key]); cur = repaired.get(key, {})
    if isinstance(cur, dict): base.update(cur)
    repaired[key] = base
server._repair_v213_settings_form_corruption(repaired)
assert repaired['recu']['enabled'] is True and repaired['recu']['browser_debug_port'] == 9223
assert repaired['mosaic_settings']['sample_every_seconds'] == 300 and repaired['mosaic_settings']['use_existing_mosaics'] is True
assert repaired['review_settings']['sample_every_seconds'] == 180 and repaired['review_settings']['use_existing_mosaics'] is True
assert repaired['library_background']['enabled'] is True
assert repaired['model_admin']['enabled'] is True and repaired['model_admin']['auto_discover'] is True
assert repaired['live_bridge']['enabled'] is True and repaired['live_bridge']['auto_discover'] is True
near_miss = copy.deepcopy(corrupt); near_miss['mosaic_settings']['sample_every_seconds'] = 123
assert not server._looks_like_v213_settings_form_corruption(near_miss)
print('PASS exact v2.13 settings-overwrite fingerprint repairs conservatively; near-miss is untouched')

# 2) Settings optimistic lock fails closed before it can touch configuration.
guard = object.__new__(server.MobileReviewerState); guard.config={'secret_key':'x','access_pin':'123456','recu':{}}
rev = server.MobileReviewerState.settings_revision_token(guard)
for payload, needle in [({}, 'not loaded'), ({'settings_revision':'stale'}, 'changed since')]:
    before = copy.deepcopy(guard.config)
    try:
        server.MobileReviewerState.update_mobile_settings(guard, payload)
        raise AssertionError('settings write unexpectedly accepted')
    except RuntimeError as exc:
        assert needle in str(exc).casefold(), exc
    assert guard.config == before
assert rev
print('PASS settings write guard rejects missing/stale form revisions without mutation')

# 3) Freshly rebuilt chunks inherit ready outputs only on exact signature.
class FakeChunk:
    def __init__(self, signature): self.signature=signature; self.mosaics=[]
h = object.__new__(server.MobileReviewerState); h.lock=threading.RLock()
h.ready_index={'snapshots':{'original:model':{'chunks':[{'signature':'same','ready':True,'mosaics':['/tmp/a.jpg']},{'signature':'other','ready':True,'mosaics':['/tmp/b.jpg']}]}}}
a,b=FakeChunk('same'),FakeChunk('changed')
count=server.MobileReviewerState._hydrate_chunks_from_ready_snapshot(h,'original','model',[a,b])
assert count==1 and [str(v) for v in a.mosaics]==['/tmp/a.jpg'] and not b.mosaics
print('PASS exact-signature hydration preserves ready mosaic references without unsafe cross-chunk reuse')

# Persisting a rebuilt Original chunk must not turn READY into not-ready merely
# because use_existing_mosaics was false when metadata was rebuilt.
with tempfile.TemporaryDirectory(prefix='ctbrec_v2133_ready_') as td0:
    td=Path(td0); image=td/'ready.jpg'; image.write_bytes(b'jpeg-placeholder')
    source=td/'source.mp4'; source.write_bytes(b'video')
    st=source.stat(); start=datetime(2026,9,10,18,0,0)
    video=mosaic.VideoInfo(source,start,st.st_size,st.st_mtime,60)
    chunk=mosaic.Chunk(1,td,[video],start,start+timedelta(seconds=60),st.st_size,key='k',signature='ready-sig')
    h2=object.__new__(server.MobileReviewerState); h2.lock=threading.RLock()
    h2.ready_index={'snapshots':{'original:model':{'chunks':[{'signature':'ready-sig','ready':True,'mosaics':[str(image)]}]}}}
    h2.offline_media_lookup={}; h2.ready_count_cache={}; h2.catalog_view_cache={}
    h2._source_identity_from_descriptors=lambda files:'id'
    h2._embedded_layout_entry=lambda mode,c,outputs:{'ok':True} if outputs else None
    h2._save_ready_index=lambda : None
    assert server.MobileReviewerState._hydrate_chunks_from_ready_snapshot(h2,'original','model',[chunk])==1
    server.MobileReviewerState._persist_ready_snapshot(h2,'original','model',[chunk],st.st_size,[])
    snap=h2.ready_index['snapshots']['original:model']
    assert snap['ready_chunks']==1 and snap['chunks'][0]['ready'] is True and snap['chunks'][0]['mosaics']==[str(image)]
print('PASS opening/rebuilding a model preserves its exact-signature READY snapshot instead of erasing it')

# 4) Recu accepts Review queues, not just Originals.
rq = object.__new__(server.MobileReviewerState); rq.lock=threading.RLock()
rq.queues={'q':{'mode':'review','model':'alice','chunks':[],'recu_status':'not_started','recu_error':'','recu_data':None}}
rq.recu_status=lambda : {'enabled':False,'session_captured':False,'browser_running':False}
assert server.MobileReviewerState.start_recu_for_queue(rq,'q',False) is None
assert rq.queues['q']['recu_status']=='disabled'  # got past mode guard and evaluated Recu access
print('PASS Review queues enter the same Recu access path as Originals')

# 5) Real Original generate_mosaic execution must not hit the v2.13 `normalized` NameError.
ffmpeg = shutil.which('ffmpeg')
if ffmpeg:
    with tempfile.TemporaryDirectory(prefix='ctbrec_v2133_mosaic_') as td0:
        td=Path(td0); folder=td/'model'; folder.mkdir()
        video=folder/'alice_2026-09-10_18-00-00.mp4'
        cmd=[ffmpeg,'-hide_banner','-loglevel','error','-f','lavfi','-i','testsrc=size=320x180:rate=10','-t','4','-pix_fmt','yuv420p','-y',str(video)]
        subprocess.run(cmd,check=True,timeout=30)
        st=video.stat(); start=datetime(2026,9,10,18,0,0)
        vi=mosaic.VideoInfo(video,start,st.st_size,st.st_mtime,4)
        chunk=mosaic.Chunk(1,folder,[vi],start,start+timedelta(seconds=4),st.st_size,key='k',signature='sig2133')
        settings=dict(mosaic.DEFAULT_SETTINGS); settings.update({'ffmpeg_path':ffmpeg,'sample_every_seconds':1,'max_total_frames':4,'columns':2,'tile_width':160,'frame_extract_batch_size':6,'use_existing_mosaics':False})
        cache=mosaic.DurationCache(td/'dur.json'); cache.set(video,st.st_size,st.st_mtime,4); cache.save()
        manifest=mosaic.MosaicManifest(td/'manifest.json')
        ok,msg,outs=mosaic.generate_mosaic(chunk,settings,cache,manifest,threading.Event(),[None],lambda *args:None,force_regenerate=True)
        assert ok, msg
        assert outs and all(Path(x).is_file() for x in outs), outs
    print('PASS complete Original generate_mosaic batching path runs with real ffmpeg (no normalized NameError)')
else:
    print('SKIP real ffmpeg mosaic regression: ffmpeg not installed in validation environment')

print('PASS v2.13.3 field regression suite')
