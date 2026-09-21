import json
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from discovery.continuity import suggest_continuations
from discovery.collectors import (
    ChaturbateAffiliateCollector, ContinuityExportCollector, NeighborExportCollector,
    RecuDiscoveryExportCollector, RecuLocalArchivesCollector, LiveControlModelsCollector, CollectorManager,
)
from discovery.engine import DiscoveryEngine
from discovery.importers import import_catalog
from discovery.integration import ReviewerIntegration
from discovery.models import Evidence, priority_to_label
from discovery.scoring import rank_recommendations
from discovery.store import DiscoveryStore


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.td=tempfile.TemporaryDirectory(); self.root=Path(self.td.name); self.store=DiscoveryStore(self.root/'d.sqlite3')
    def tearDown(self): self.td.cleanup()

    def test_priority_mapping(self):
        self.assertEqual(priority_to_label(1000),'favorite'); self.assertEqual(priority_to_label(800),'likely_favorite')
        self.assertEqual(priority_to_label('Low Priority'),'low_priority'); self.assertEqual(priority_to_label('paused'),'unsorted')

    def test_catalog_import_skips_unchanged_file_after_first_v4_read(self):
        p=self.root/'catalog.json'
        p.write_text(json.dumps({'catalog':{'alpha':[{'bytes':100}]}}))
        r1=import_catalog(p,self.store); r2=import_catalog(p,self.store)
        self.assertEqual(r1['imported'],1); self.assertEqual(r2.get('unchanged'),1)
        self.assertEqual(len(self.store.list_accounts()),1)

    def test_catalog_import_camelcase_and_operational_state(self):
        p=self.root/'catalog.json'
        p.write_text(json.dumps({'models':[{'modelName':'alpha','priority':1000,'tags':['a','b'],'lastSeen':'2026-09-20T10:00:00Z','totalSizeBytes':123,'driveSizes':{'E:':100,'F:':23},'state':'paused'},{'username':'beta','priority':50}]}))
        r=import_catalog(p,self.store); self.assertEqual(r['imported'],2)
        rows={x['username']:x for x in self.store.list_accounts()}
        self.assertEqual(rows['alpha']['preference_label'],'favorite')
        self.assertEqual(rows['alpha']['metadata']['size_bytes'],123)
        self.assertEqual(rows['alpha']['metadata']['per_drive']['E:'],100)

    def test_neighbor_ranking_and_feedback(self):
        _,a=self.store.upsert_identity_account('chaturbate','seed',preference_label='favorite',metadata={'tags':['athletic','tattoo']})
        ib,b=self.store.upsert_identity_account('chaturbate','candidate',metadata={'tags':['athletic','tattoo']})
        self.store.upsert_identity_account('chaturbate','other',metadata={'tags':['other']})
        self.store.add_evidence(Evidence(kind='similar_model',source='test',subject_account_id=a,related_account_id=b,payload={'appearances':8,'opportunities':10}))
        rows=rank_recommendations(self.store); self.assertEqual(rows[0].username,'candidate'); self.assertGreater(rows[0].score,50)
        event=self.store.set_preference(ib,'continue'); self.assertTrue(event)
        self.assertEqual(self.store.get_account(b)['preference_label'],'continue')

    def test_evidence_deduplicates_current_signal(self):
        _,a=self.store.upsert_identity_account('chaturbate','candidate')
        first=self.store.add_evidence(Evidence(kind='recu_bookmark_rank',source='recu',subject_account_id=a,payload={'period':'week','rank':9,'total':100}))
        second=self.store.add_evidence(Evidence(kind='recu_bookmark_rank',source='recu',subject_account_id=a,payload={'period':'week','rank':2,'total':100}))
        self.assertEqual(first,second)
        self.assertEqual(self.store.evidence_count(),1)
        self.assertEqual(self.store.all_evidence()[0]['payload']['rank'],2)

    def test_recu_signal_cannot_be_only_signal_to_force_100(self):
        _,a=self.store.upsert_identity_account('chaturbate','candidate')
        self.store.add_evidence(Evidence(kind='recu_bookmark_rank',source='recu',subject_account_id=a,payload={'period':'week','rank':1,'total':1000}))
        r=rank_recommendations(self.store)[0]; self.assertLess(r.score,90)

    def test_stale_recu_signal_decays(self):
        _,fresh=self.store.upsert_identity_account('chaturbate','fresh')
        _,stale=self.store.upsert_identity_account('chaturbate','stale')
        self.store.add_evidence(Evidence(kind='recu_bookmark_rank',source='recu',subject_account_id=fresh,payload={'period':'week','rank':1,'total':100}))
        old=(datetime.now(timezone.utc)-timedelta(days=60)).replace(microsecond=0).isoformat()
        self.store.add_evidence(Evidence(kind='recu_bookmark_rank',source='recu',subject_account_id=stale,observed_at=old,payload={'period':'week','rank':1,'total':100}))
        rows={r.username:r for r in rank_recommendations(self.store)}
        self.assertGreater(rows['fresh'].components['recu_momentum'], rows['stale'].components['recu_momentum'])

    def test_official_redirect_continuity_and_persistent_reject(self):
        io,old=self.store.upsert_identity_account('chaturbate','oldname',preference_label='favorite')
        inew,new=self.store.upsert_identity_account('chaturbate','newname')
        self.store.add_evidence(Evidence(kind='official_redirect',source='chaturbate',subject_account_id=old,related_account_id=new))
        rows=suggest_continuations(self.store); self.assertEqual(rows[0]['confidence_band'],'A'); self.assertGreater(rows[0]['score'],.95)
        self.store.set_identity_link_decision(io,inew,'rejected',reasons=['test'])
        self.assertEqual(suggest_continuations(self.store),[])

    def test_feedback_undo_and_outbox(self):
        ident,acct=self.store.upsert_identity_account('chaturbate','candidate')
        e=DiscoveryEngine({'database_path':'engine.sqlite3','external_evidence_inbox':'state/inbox','mobile_catalog_cache':'','favorite_models_file':'','continue_models_file':'','default_platform':'chaturbate','sync_interval_seconds':999,'queue_preference_actions':True,'reviewer_base_url':'http://127.0.0.1:1','reviewer_probe_timeout_seconds':.1},self.root)
        ident2,acct2=e.store.upsert_identity_account('chaturbate','candidate')
        out=e.set_feedback(ident2,'favorite')
        self.assertTrue(out['outbox_action_id'])
        self.assertEqual(e.store.get_account(acct2)['preference_label'],'favorite')
        undone=e.undo_last_feedback()
        self.assertEqual(undone['restored_label'],'unsorted')
        self.assertEqual(e.store.get_account(acct2)['preference_label'],'unsorted')
        self.assertEqual(e.store.action_counts()['pending'],2)

    def test_engine_inbox(self):
        inbox=self.root/'state'/'inbox'; inbox.mkdir(parents=True)
        (inbox/'x.jsonl').write_text(json.dumps({'kind':'recu_bookmark_rank','source':'recu','subject':{'username':'x'},'payload':{'period':'week','rank':2,'total':100}})+'\n')
        e=DiscoveryEngine({'database_path':'state/d.sqlite3','external_evidence_inbox':'state/inbox','mobile_catalog_cache':'','favorite_models_file':'','continue_models_file':'','default_platform':'chaturbate','sync_interval_seconds':999,'queue_preference_actions':True,'reviewer_base_url':'http://127.0.0.1:1','reviewer_probe_timeout_seconds':.1},self.root)
        r=e.ingest_inbox(); self.assertEqual(r['records'],1); self.assertTrue((inbox/'processed'/'x.jsonl').exists())


    def test_seen_timestamps_are_monotonic(self):
        self.store.upsert_identity_account('chaturbate','alpha',first_seen='2026-09-10T00:00:00+00:00',last_seen='2026-09-15T00:00:00+00:00')
        self.store.upsert_identity_account('chaturbate','alpha',first_seen='2026-09-12T00:00:00+00:00',last_seen='2026-09-14T00:00:00+00:00')
        row=self.store.find_account('chaturbate','alpha')
        self.assertEqual(row['first_seen'],'2026-09-10T00:00:00+00:00')
        self.assertEqual(row['last_seen'],'2026-09-15T00:00:00+00:00')
        self.store.upsert_identity_account('chaturbate','alpha',first_seen='2026-09-01T00:00:00+00:00',last_seen='2026-09-20T00:00:00+00:00')
        row=self.store.find_account('chaturbate','alpha')
        self.assertEqual(row['first_seen'],'2026-09-01T00:00:00+00:00')
        self.assertEqual(row['last_seen'],'2026-09-20T00:00:00+00:00')

    def test_affiliate_collector_paginates_filters_and_discovers_new(self):
        self.store.upsert_identity_account('chaturbate','known',preference_label='continue')
        calls=[]
        pages=[
            {'results':[
                {'username':'known','current_show':'public','is_new':False,'num_users':44,'tags':['athletic']},
                {'username':'brandnew','current_show':'public','is_new':True,'num_users':12,'tags':['tattoo']},
            ],'next':'yes'},
            {'results':[
                {'username':'ordinary','current_show':'public','is_new':False},
                {'username':'private_new','current_show':'private','is_new':True},
            ],'next':None},
        ]
        def fake(url,timeout):
            calls.append(url); return pages[len(calls)-1]
        c=ChaturbateAffiliateCollector(self.store,{'enabled':True,'wm':'SECRET-WM','page_size':2,'max_pages':10,'candidate_mode':'new_and_known'},fetch_json=fake)
        r=c.run().as_dict()
        self.assertEqual(r['pages'],2); self.assertEqual(r['imported'],2); self.assertEqual(r['new_candidates'],1)
        self.assertIsNotNone(self.store.find_account('chaturbate','known'))
        self.assertIsNotNone(self.store.find_account('chaturbate','brandnew'))
        self.assertIsNone(self.store.find_account('chaturbate','ordinary'))
        self.assertIsNone(self.store.find_account('chaturbate','private_new'))
        ev=[x for x in self.store.all_evidence() if x['kind']=='new_account_seen']
        self.assertEqual(len(ev),1); self.assertEqual(self.store.get_account(ev[0]['subject_account_id'])['username'],'brandnew')
        self.assertTrue(any('offset=0' in u for u in calls)); self.assertTrue(any('offset=2' in u for u in calls))

    def test_affiliate_status_never_exposes_wm(self):
        cfg={'collectors':{'chaturbate_affiliate':{'enabled':True,'wm':'TOP-SECRET','poll_seconds':300}}}
        m=CollectorManager(self.store,self.root,cfg)
        blob=json.dumps(m.status())
        self.assertNotIn('TOP-SECRET',blob)
        self.assertIn('configured',blob)

    def test_recu_discovery_export_is_deduped(self):
        p=self.root/'recu.json'
        p.write_text(json.dumps({'rows':[{'username':'alpha','period':'week','rank':2,'total':100,'clips_7d':7,'momentum':0.8}]}))
        c=RecuDiscoveryExportCollector(self.store,self.root,{'enabled':True,'path':str(p)})
        r1=c.run(); self.store.set_source_state(c.source,status=r1.status,detail='',cursor=r1.cursor,success=r1.success)
        before=self.store.evidence_count(); r2=c.run()
        self.assertEqual(before,3); self.assertEqual(r2.status,'unchanged'); self.assertEqual(self.store.evidence_count(),before)

    def test_neighbor_export_drives_personalized_candidate(self):
        self.store.upsert_identity_account('chaturbate','seed',preference_label='favorite')
        p=self.root/'neighbors.csv'
        p.write_text('seed_username,candidate_username,source,appearances,opportunities\nseed,candidate,authorized_neighbor_export,8,10\n')
        c=NeighborExportCollector(self.store,self.root,{'enabled':True,'path':str(p)})
        self.assertEqual(c.run().status,'ok')
        rows=rank_recommendations(self.store)
        self.assertEqual(rows[0].username,'candidate'); self.assertGreater(rows[0].components['neighbor'],0.4)

    def test_continuity_export_creates_band_a_redirect(self):
        self.store.upsert_identity_account('chaturbate','oldname',preference_label='favorite')
        p=self.root/'continuity.csv'
        p.write_text('old_username,new_username,source\noldname,newname,official_public_redirect\n')
        c=ContinuityExportCollector(self.store,self.root,{'enabled':True,'path':str(p)})
        self.assertEqual(c.run().status,'ok')
        rows=suggest_continuations(self.store)
        self.assertEqual(rows[0]['new_username'],'newname'); self.assertEqual(rows[0]['confidence_band'],'A')

    def test_recu_local_archive_only_enriches_known_accounts(self):
        self.store.upsert_identity_account('chaturbate','known')
        p=self.root/'archive.json'
        p.write_text(json.dumps({'known':{'last_broadcast':'2026-09-19T12:00:00+00:00','recordings':12,'clips':30},'unknown':{'last_broadcast':'2026-09-20T12:00:00+00:00','recordings':99}}))
        c=RecuLocalArchivesCollector(self.store,self.root,{'enabled':True,'paths':[str(p)]})
        r=c.run(); self.assertEqual(r.status,'ok'); self.assertEqual(r.detail['imported'],1)
        known=self.store.find_account('chaturbate','known')
        self.assertEqual(known['last_seen'],'2026-09-19T12:00:00+00:00')
        self.assertEqual(known['metadata']['recu_recordings'],12)
        self.assertIsNone(self.store.find_account('chaturbate','unknown'))


    def test_real_mobile_catalog_map_with_list_values_imports_models(self):
        p=self.root/'mobile_catalog_cache.json'
        p.write_text(json.dumps({
            'updated_at':'2026-09-20T18:00:00Z','roots_file':'recording_roots.txt',
            'catalog':{
                'alpha':[{'folder':'E:/Recordings/alpha','bytes':100,'file_count':2}],
                'beta':[{'folder':'F:/Recordings/beta','size_bytes':250,'file_count':3}],
            }
        }))
        r=import_catalog(p,self.store)
        self.assertEqual(r['seen'],2); self.assertEqual(r['imported'],2)
        rows={x['username']:x for x in self.store.list_accounts()}
        self.assertEqual(rows['alpha']['metadata']['size_bytes'],100)
        self.assertEqual(rows['beta']['metadata']['folder_count'],1)

    def test_test_label_is_neutral_not_positive_seed(self):
        from discovery.models import PREFERENCE_WEIGHTS
        self.assertEqual(PREFERENCE_WEIGHTS['test'],0.0)

    def test_global_gender_filter_excludes_known_female_and_couples(self):
        self.store.upsert_identity_account('chaturbate','male',metadata={'gender':'m'})
        self.store.upsert_identity_account('chaturbate','female',metadata={'gender':'f'})
        self.store.upsert_identity_account('chaturbate','couple',metadata={'gender':'c'})
        self.store.upsert_identity_account('chaturbate','unknown')
        rows=rank_recommendations(self.store,filters={'allowed_genders':['m'],'include_unknown_gender':True,'include_couples':False})
        names={r.username for r in rows}
        self.assertIn('male',names); self.assertIn('unknown',names)
        self.assertNotIn('female',names); self.assertNotIn('couple',names)
        # Unchecking every known-gender toggle means no known m/f/t accounts, not 'all'.
        rows=rank_recommendations(self.store,filters={'allowed_genders':[],'include_unknown_gender':True,'include_couples':False})
        self.assertEqual({r.username for r in rows},{'unknown'})

    def test_live_control_collector_reuses_loopback_payload_and_priority(self):
        payload={'models':[
            {'name':'seed','priority':1000,'online':True,'affiliateRoomInfo':{'gender':'m','tags':['athletic'],'num_users':42,'snapshot_ms':123}},
            {'name':'other','priority':50,'online':False},
        ]}
        c=LiveControlModelsCollector(self.store,{'enabled':True,'base_url':'http://127.0.0.1:8792'},fetch_json=lambda url,timeout: payload)
        r=c.run(); self.assertEqual(r.status,'ok'); self.assertEqual(r.detail['imported'],2)
        seed=self.store.find_account('chaturbate','seed')
        self.assertEqual(seed['preference_label'],'favorite')
        self.assertEqual(seed['metadata']['gender'],'m')
        self.assertTrue(seed['metadata']['live_control_present'])

    def test_recu_local_archive_reports_parse_match_skip_diagnostics(self):
        self.store.upsert_identity_account('chaturbate','known')
        p=self.root/'recu_local_archive.json'
        p.write_text(json.dumps({'version':1,'models':{
            'known':{'fetched_at':'2026-09-20T12:00:00','moments':[{'x':1},{'x':2}],'video_meta':{'1':{},'2':{},'3':{}}},
            'unknown':{'moments':[{}]},
        }}))
        c=RecuLocalArchivesCollector(self.store,self.root,{'enabled':True,'paths':[str(p)]})
        r=c.run()
        self.assertEqual(r.detail['parsed_records'],2); self.assertEqual(r.detail['matched_known'],1); self.assertEqual(r.detail['skipped_unknown'],1)
        row=self.store.find_account('chaturbate','known')
        self.assertEqual(row['metadata']['recu_moments'],2); self.assertEqual(row['metadata']['recu_recordings'],3)

    def test_reviewer_probe_treats_auth_boundary_as_reachable(self):
        class H(BaseHTTPRequestHandler):
            def do_HEAD(self): self.send_response(401); self.end_headers()
            def log_message(self,*args): pass
        srv=HTTPServer(('127.0.0.1',0),H); t=threading.Thread(target=srv.serve_forever,daemon=True);t.start()
        try:
            i=ReviewerIntegration({'reviewer_base_url':f'http://127.0.0.1:{srv.server_port}','reviewer_probe_timeout_seconds':1,'mobile_catalog_cache':''},self.root)
            r=i.probe_reviewer(); self.assertTrue(r['reachable']); self.assertTrue(r['auth_required']); self.assertEqual(r['status'],401)
        finally:
            srv.shutdown(); srv.server_close()


    def test_nested_three_bucket_mobile_catalog_imports_unique_models_and_cleans_v040_artifacts(self):
        # Simulate the exact field symptom: catalog has three outer structural buckets.
        for bogus in ('original','review','deletion'):
            self.store.upsert_identity_account('chaturbate', bogus, source='mobile_reviewer_catalog')
        p=self.root/'mobile_catalog_cache.json'
        p.write_text(json.dumps({
            'updated_at':'2026-09-20T19:00:00Z','roots_file':'recording_roots.txt',
            'catalog':{
                'original':[
                    {'model_name':'alpha','folder':'E:/Recordings/alpha','bytes':100},
                    {'model_name':'beta','folder':'F:/Recordings/beta','bytes':200},
                ],
                'review':{
                    'alpha':[{'folder':'E:/Recordings/alpha/Review','bytes':30}],
                    'gamma':[{'folder':'G:/Recordings/gamma/Review','bytes':40}],
                },
                'deletion':{
                    'G:/MARKED_FOR_DELETION':{
                        'delta':[{'folder':'G:/MARKED_FOR_DELETION/delta','bytes':50}]
                    }
                },
            }
        }))
        r=import_catalog(p,self.store)
        self.assertEqual(r['imported'],4, r)
        self.assertEqual(r['cleaned_artifacts'],3, r)
        names={x['username'] for x in self.store.list_accounts()}
        self.assertTrue({'alpha','beta','gamma','delta'}.issubset(names), names)
        self.assertFalse({'original','review','deletion'}.intersection(names), names)
        self.assertEqual(self.store.find_account('chaturbate','alpha')['metadata']['size_bytes'],130)
        # v41 parser generation must force a re-read even if v0.4 incorrectly marked this file done.
        st=p.stat(); self.store.set_source_state('mobile_reviewer_catalog',status='ok',detail='old 3/3',cursor=f'v4:{st.st_mtime_ns}:{st.st_size}',success=True)
        r2=import_catalog(p,self.store)
        self.assertNotEqual(r2.get('unchanged'),1)

    def test_recu_v46_cursor_forces_reparse_after_stale_install_and_matches_catalog_accounts(self):
        self.store.upsert_identity_account('chaturbate','known',source='mobile_reviewer_catalog')
        p=self.root/'recu_local_archive.json'
        p.write_text(json.dumps({'models':{'known':{'fetched_at':'2026-09-20T12:00:00','moments':[{},{}],'video_meta':{'1':{}}}}}))
        old='v4|'+f'{p}:{p.stat().st_mtime_ns}:{p.stat().st_size}'
        self.store.set_source_state('recu_local_archives',status='ok',detail='old zero',cursor=old,success=True)
        c=RecuLocalArchivesCollector(self.store,self.root,{'enabled':True,'paths':[str(p)]})
        r=c.run()
        self.assertEqual(r.status,'ok'); self.assertEqual(r.detail['matched_known'],1); self.assertEqual(r.detail['imported'],1)
        self.assertTrue(str(r.cursor).startswith('v46|'))

    def test_collector_busy_is_per_source_not_global(self):
        class H(BaseHTTPRequestHandler):
            def do_GET(self):
                body=json.dumps({'models':[{'name':'loopback','priority':50}]}).encode()
                self.send_response(200); self.send_header('Content-Length',str(len(body))); self.end_headers(); self.wfile.write(body)
            def log_message(self,*args): pass
        srv=HTTPServer(('127.0.0.1',0),H); t=threading.Thread(target=srv.serve_forever,daemon=True); t.start()
        try:
            cfg={'live_control_base_url':f'http://127.0.0.1:{srv.server_port}','collectors':{
                'live_control_models':{'enabled':True,'poll_seconds':60},
                'recu_local_archives':{'enabled':True,'paths':[],'poll_seconds':600},
            }}
            m=CollectorManager(self.store,self.root,cfg)
            m._locks['recu_local_archives'].acquire()
            try:
                r=m.run('live_control_models',force=True)
            finally:
                m._locks['recu_local_archives'].release()
            self.assertEqual(r['status'],'ok',r)
            self.assertIsNotNone(self.store.find_account('chaturbate','loopback'))
        finally:
            srv.shutdown(); srv.server_close()

    def test_reviewer_probe_prefers_lightweight_api_auth_over_shell_get(self):
        calls=[]
        class H(BaseHTTPRequestHandler):
            def do_GET(self):
                calls.append(self.path)
                if self.path=='/api/auth':
                    body=b'{"authenticated":true,"local":true}'
                    self.send_response(200); self.send_header('Server','CTBRecMobile/2.15.5'); self.send_header('Content-Length',str(len(body))); self.end_headers(); self.wfile.write(body); return
                self.send_response(500); self.end_headers()
            def do_HEAD(self): calls.append('HEAD '+self.path); self.send_response(501); self.end_headers()
            def log_message(self,*args): pass
        srv=HTTPServer(('127.0.0.1',0),H); t=threading.Thread(target=srv.serve_forever,daemon=True);t.start()
        try:
            i=ReviewerIntegration({'reviewer_base_url':f'http://127.0.0.1:{srv.server_port}','reviewer_probe_timeout_seconds':0.1,'mobile_catalog_cache':''},self.root)
            r=i.probe_reviewer(); self.assertTrue(r['reachable'],r); self.assertEqual(r['status'],200); self.assertEqual(r['probe_method'],'GET /api/auth')
            self.assertEqual(calls,['/api/auth'])
        finally:
            srv.shutdown(); srv.server_close()

if __name__=='__main__': unittest.main()
