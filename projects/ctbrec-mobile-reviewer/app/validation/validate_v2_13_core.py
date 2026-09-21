import json, sys, tempfile, threading, time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import ctbrec_mobile_server as server
import ctbrec_mobile_model_admin as admin_mod

# 1. Durable paired token is installation-bound to secret_key.
obj=object.__new__(server.MobileReviewerState)
obj.config={'secret_key':'alpha'}
a=obj.device_token_value
obj.config={'secret_key':'beta'}
b=obj.device_token_value
assert a and b and a != b
obj.config={'secret_key':'alpha'}
assert obj.device_token_value == a
print('PASS paired-device token changes when installation secret changes')

# 2. Model-admin filter cache: unchanged files are not reparsed, mutations invalidate.
with tempfile.TemporaryDirectory() as td0:
    td=Path(td0)
    models=td/'models.json'
    models.write_text(json.dumps({'models':[{'name':'alice','url':'https://chaturbate.com/alice/'}], 'modelNotes':{'https://chaturbate.com/alice/':'CTBRec Sorter EZ Sort: yes'}}),encoding='utf-8')
    adm=admin_mod.MobileModelAdmin(td, {'enabled':True,'auto_discover':False,'models_json_paths':[str(models)]})
    assert 'alice' in adm.easy_sort_names()
    builds1=adm.filter_cache_debug()['builds']
    assert 'alice' in adm.easy_sort_names()
    builds2=adm.filter_cache_debug()['builds']
    assert builds2 == builds1, (builds1,builds2)
    time.sleep(0.01)
    models.write_text(json.dumps({'models':[{'name':'alice','url':'https://chaturbate.com/alice/'}], 'modelNotes':{}}),encoding='utf-8')
    # v2.13.3 imports legacy EZ Sort membership into Reviewer-owned metadata,
    # so a later controller/path/note disappearance cannot erase the tab.
    assert 'alice' in adm.easy_sort_names()
    builds3=adm.filter_cache_debug()['builds']
    assert builds3 >= builds2 + 1, (builds2,builds3)
    meta=json.loads((td/'mobile_model_metadata.json').read_text(encoding='utf-8'))
    assert 'alice' in {str(v).casefold() for v in meta.get('easy_sort_imported', [])}
    adm.configure({'enabled':False,'auto_discover':False,'models_json_paths':[str(models)]})
    assert 'alice' in adm.easy_sort_names()
    print('PASS EZ Sort membership is imported durably and survives native-controller disable/path churn')

# Helpers for bare state unit tests.
class DummyAdmin:
    def __init__(self): self.token=('admin',1); self.hidden=set(); self.ignored=set(); self.easy=set()
    def filter_cache_token(self): return self.token
    def hidden_names(self): return set(self.hidden)
    def ignored_names(self): return set(self.ignored)
    def easy_sort_names(self): return set(self.easy)

state=object.__new__(server.MobileReviewerState)
state.lock=threading.RLock()
state.catalog={'original':[{'name':'A','bytes':100,'folder':'/tmp/a','drive':'E'},{'name':'B','bytes':50,'folder':'/tmp/b','drive':'E'}], 'review':[], 'deletion':[]}
state.catalog_status={'updated_at':'cat1'}
state.ready_index={'updated_at':'ready1','snapshots':{}}
state.catalog_view_cache={}; state.ready_count_cache={}
state.model_admin=DummyAdmin()
state.action_queue={'jobs':[]}
ready_calls={'n':0}
def ready_counts(mode,drives):
    ready_calls['n']+=1
    return {'a':2}
state._ready_counts_for_mode=ready_counts
rows1=server.MobileReviewerState.catalog_models(state,'original',set(),'')
rows2=server.MobileReviewerState.catalog_models(state,'original',set(),'')
assert rows1 == rows2 and ready_calls['n']==1, ready_calls
state.model_admin.token=('admin',2)
rows3=server.MobileReviewerState.catalog_models(state,'original',set(),'')
assert ready_calls['n']==2 and rows3==rows1
state.catalog_status={'updated_at':'cat2'}
rows4=server.MobileReviewerState.catalog_models(state,'original',set(),'')
assert ready_calls['n']==3 and rows4==rows1
print('PASS server catalog view cache returns equivalent rows and invalidates on admin/catalog changes')

# 3. Lightweight status must never ask layout geometry machinery to open JPEGs.
class Chunk:
    signature='sig'; mosaics=[Path('/tmp/nonexistent')]
chunk=Chunk()
qs=object.__new__(server.MobileReviewerState)
qs.lock=threading.RLock(); qs.queues={'q':{'mode':'original','model':'M','chunks':[chunk],'initial_count':1,'history':[], 'current_mosaic_status':{'signature':'sig','state':'ready','message':'ready'}}}
qs.current_chunk=lambda q: q['chunks'][0] if q['chunks'] else None
qs.recu_markers_for_chunk=lambda q,c: {'status':'ready','segments':{},'unmatched':[],'error':''}
qs.prefetch_status=lambda q: {'state':'idle'}
qs.action_queue_summary=lambda : {}
qs.library_status_payload=lambda : {}
qs.nsfw_status_payload=lambda : {}
qs._scheduler_settings=lambda : {}
qs.layout_parts=lambda *a,**k: (_ for _ in ()).throw(AssertionError('layout_parts must not run'))
payload=server.MobileReviewerState.queue_status_payload(qs,'q')
assert payload['chunk_signature']=='sig'
print('PASS lightweight queue status does not build/read mosaic layout geometry')

# 4. open-fast validates only the configured warm set, never the whole snapshot.
class DChunk:
    def __init__(self,sig): self.signature=sig; self.folder=Path('/tmp'); self.mosaics=[Path('/tmp/fake.jpg')]
of=object.__new__(server.MobileReviewerState)
of.lock=threading.RLock(); of.interactive_demand=threading.Event(); of.queues={}
of.config={'speed_mode':{'open_fast_initial_chunks':3},'recu':{'enabled':False}}
of.ready_index={'snapshots':{'original:model':{'chunks':[{'signature':f's{i}','ready':True,'mosaics':['x.jpg'],'source_bytes':1000-i,'folder':'/tmp'} for i in range(10)]}}}
of._pending_chunk_signatures=lambda : set()
of._deserialize_chunk=lambda mode,row: DChunk(row['signature'])
of._chunk_is_claimed=lambda chunk: False
checks={'n':0}
def mosaic_ready(q,chunk): checks['n']+=1; return True
of._chunk_mosaic_ready=mosaic_ready
of.mark_sort_session_active=lambda *a,**k: {'ok':True}
of.start_recu_for_queue=lambda *a,**k: None
of.current_payload=lambda qid: {'done':False,'queue_id':qid,'chunk':{'signature':'s0','parts':[1]}}
result=server.MobileReviewerState.quick_load_queue(of,'original','model',set())
assert result['ready'] and result['ready_chunks']==10, result
assert checks['n']==3, checks
assert len(of.queues[result['queue_id']]['chunks']) == 10
print('PASS open-fast validates only 3 warm mosaics but preserves all 10 durable-ready chunks')
