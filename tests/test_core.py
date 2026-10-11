import fcntl, hashlib, json, os, subprocess, sys, tempfile, unittest, tomllib
from pathlib import Path
from unittest.mock import patch
from jepasearch.core import index_video, search, decide, references, SearchError, connect
from jepasearch.synthetic import make_video
from jepasearch.model import load_raw_vjepa2

class SearchCoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='jepa-synth-test-')
        self.root=Path(self.tmp.name)
        self.video=make_video(self.root/'generated.mp4',duration=6)
        self.store=self.root/'index.sqlite'
    def tearDown(self): self.tmp.cleanup()
    def test_synthetic_index_query_and_decision(self):
        before=hashlib.sha256(self.video.read_bytes()).hexdigest()
        result=index_video(self.video,self.store,reserve_bytes=0)
        self.assertEqual(result['windows_added'],9)
        rows=search(self.video,0,2,self.store,limit=5)
        self.assertTrue(rows)
        self.assertEqual(rows[0]['start_s'],0.0)
        self.assertGreaterEqual(rows[0]['score'],0.99)
        self.assertTrue(all(abs(a['start_s']-b['start_s'])>=1 for i,a in enumerate(rows) for b in rows[i+1:] if a['media_id']==b['media_id']))
        decide(self.store,rows[0]['media_id'],rows[0]['start_s'],'keep')
        self.assertEqual(references(self.store)[0]['decision'],'keep')
        self.assertEqual(hashlib.sha256(self.video.read_bytes()).hexdigest(),before)
    def test_incremental_resume_and_explicit_duplicate_prevention(self):
        first=index_video(self.video,self.store,reserve_bytes=0)
        second=index_video(self.video,self.store,reserve_bytes=0)
        self.assertEqual(first['windows_added'],9); self.assertEqual(second['windows_added'],0)
        with self.assertRaisesRegex(SearchError,'already indexed'): index_video(self.video,self.store,resume=False,reserve_bytes=0)
    def test_partial_index_resumes_missing_windows(self):
        index_video(self.video,self.store,reserve_bytes=0)
        db=connect(self.store); db.execute("DELETE FROM windows WHERE start_s>=3.5"); db.execute("UPDATE media SET status='indexing'"); db.commit(); db.close()
        resumed=index_video(self.video,self.store,reserve_bytes=0)
        self.assertEqual(resumed['windows_added'],2)
    def test_version_dimensions_are_saved_separately(self):
        index_video(self.video,self.store,reserve_bytes=0)
        db=connect(self.store); row=db.execute('SELECT DISTINCT model_version,preprocess_version,window_version FROM windows').fetchone(); db.close()
        self.assertEqual(row,('none-baseline-v1','rgb-grid-4x4-motion-v1','2s-stride-0.5s-samples-8-terminal-2frame-retry-v2'))
    def test_low_space_and_source_overwrite_refused(self):
        with self.assertRaisesRegex(SearchError,'insufficient free space'): index_video(self.video,self.store,reserve_bytes=10**20)
        with self.assertRaisesRegex(SearchError,'overwrite'): connect(self.video,self.video)
    def test_search_refuses_store_aliases_of_query_video_without_chmod(self):
        import stat
        self.video.chmod(0o644)
        alias=self.root/'query-hardlink.sqlite'
        os.link(self.video,alias)
        for store in (self.video,alias):
            before=(hashlib.sha256(self.video.read_bytes()).digest(),self.video.stat().st_size,
                    stat.S_IMODE(self.video.stat().st_mode))
            with self.subTest(store=store.name):
                with self.assertRaisesRegex(SearchError,'overwrite the source video'):
                    search(self.video,0,2,store)
            after=(hashlib.sha256(self.video.read_bytes()).digest(),self.video.stat().st_size,
                   stat.S_IMODE(self.video.stat().st_mode))
            self.assertEqual(after,before)
        self.assertEqual(os.path.samefile(alias,self.video),True)
    def test_database_permission_restriction_rechecks_open_inode(self):
        import stat
        alias=self.root/'raced-hardlink.sqlite'
        os.link(self.video,alias)
        self.video.chmod(0o644)
        before=(hashlib.sha256(self.video.read_bytes()).digest(),self.video.stat().st_size,
                stat.S_IMODE(self.video.stat().st_mode))
        # Model a path replaced with a hardlink after the earlier path check.
        with patch('jepasearch.core._db_path',return_value=alias):
            with self.assertRaisesRegex(SearchError,'overwrite the source video'):
                connect(alias,self.video)
        after=(hashlib.sha256(self.video.read_bytes()).digest(),self.video.stat().st_size,
               stat.S_IMODE(self.video.stat().st_mode))
        self.assertEqual(after,before)
    def test_second_process_lock_refused(self):
        lock=Path(str(self.store.absolute())+'.lock'); lock.parent.mkdir(parents=True,exist_ok=True)
        with lock.open('a') as f:
            fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)
            with self.assertRaisesRegex(SearchError,'another indexing run'): index_video(self.video,self.store,reserve_bytes=0)
    def test_cli_offline_synthetic_workflow(self):
        project=Path(__file__).resolve().parents[1]
        sys_paths=[entry for entry in sys.path if entry]
        env={**os.environ,'PYTHONPATH':os.pathsep.join([str(project/'tests'/'offline_guard'),str(project/'src'),*sys_paths])}
        cli=[sys.executable,'-m','jepasearch.cli']
        generated=self.root/'cli-generated.mp4'; store=self.root/'cli-index.sqlite'
        subprocess.run(cli+['synth','--output',str(generated)],env=env,capture_output=True,text=True,check=True)
        indexed=subprocess.run(cli+['index',str(generated),'--store',str(store)],env=env,capture_output=True,text=True,check=True)
        self.assertEqual(json.loads(indexed.stdout)['windows_added'],15)
        matches=subprocess.run(cli+['search',str(generated),'--start','0','--end','2','--store',str(store)],env=env,capture_output=True,text=True,check=True)
        first=json.loads(matches.stdout)[0]
        self.assertEqual(first['device'],'OpenCV CPU')
        subprocess.run(cli+['decide','--store',str(store),'--media-id',first['media_id'],'--start',str(first['start_s']),'--decision','keep'],env=env,capture_output=True,text=True,check=True)
        refs=subprocess.run(cli+['references','--store',str(store)],env=env,capture_output=True,text=True,check=True)
        self.assertTrue(json.loads(refs.stdout)[0]['pipeline'].startswith('color-motion-'))
    def test_synthetic_generator_refuses_existing_source(self):
        target=self.root/'keep-this.bin'; target.write_bytes(b'user-owned-content')
        with self.assertRaisesRegex(FileExistsError,'refusing overwrite'): make_video(target)
        self.assertEqual(target.read_bytes(),b'user-owned-content')
        blocked=self.root/'Movies'/'must-not-create.mp4'
        with self.assertRaisesRegex(ValueError,'not allowed under a Movies directory'): make_video(blocked)
        self.assertFalse(blocked.parent.exists())
    def test_model_dependencies_are_separate_from_baseline(self):
        project=Path(__file__).resolve().parents[1]
        config=tomllib.loads((project/'pyproject.toml').read_text())['project']
        self.assertTrue(all(not item.startswith(('torch==','transformers==','Pillow==')) for item in config['dependencies']))
        self.assertEqual(config['optional-dependencies']['model'],['Pillow==11.2.1','torch==2.7.1','torchvision==0.22.1','transformers==4.53.3'])
    def test_missing_model_stops_locally(self):
        with self.assertRaisesRegex(SearchError,'configured local V-JEPA2 raw checkpoint is unavailable'):
            load_raw_vjepa2(str(self.root/'missing-model'))
    def test_vjepa_readiness_blocks_missing_local_registration_without_reading_checkpoint_bytes(self):
        import jepasearch.model as model
        checkpoint=self.root/'synthetic-checkpoint-placeholder'
        checkpoint.write_bytes(b'test marker only')
        identity=self.root/'identity.json'
        with patch.object(model,'_default_paths',return_value=(self.root,checkpoint)), \
             patch.dict(os.environ,{'JEPA_MODEL_IDENTITY_PATH':str(identity),'JEPA_RAW_CHECKPOINT_SHA256':''}), \
             patch.object(model,'_verify_pinned_loader'), \
             patch.object(model,'_installed_model_versions',return_value=dict(model.PINNED_MODEL_PACKAGES)), \
             patch.object(model,'_sha256',side_effect=AssertionError('readiness must not read checkpoint bytes')):
            status=model.vjepa2_readiness()
        self.assertFalse(status['available'])
        self.assertFalse(status['checkpoint_hash_configured'])
        self.assertFalse(status['hash_source_verified'])
        self.assertIn('明示的なローカルSHA-256登録',status['message'])
    def test_vjepa_readiness_distinguishes_local_integrity_pin_from_provider_authentication(self):
        import jepasearch.model as model
        import json, hashlib
        checkpoint=self.root/'synthetic-checkpoint-placeholder'
        checkpoint.write_bytes(b'test marker only')
        digest=hashlib.sha256(b'test marker only').hexdigest()
        identity=self.root/'identity.json'
        identity.write_text(json.dumps({
            'schema_version':1,'checkpoint_path':str(checkpoint),'sha256':digest,
            'source':{'description':'synthetic test fixture source'}
        }))
        with patch.object(model,'_default_paths',return_value=(self.root,checkpoint)), \
             patch.dict(os.environ,{'JEPA_MODEL_IDENTITY_PATH':str(identity),'JEPA_RAW_CHECKPOINT_SHA256':digest}), \
             patch.object(model,'_verify_pinned_loader'), \
             patch.object(model,'_installed_model_versions',return_value=dict(model.PINNED_MODEL_PACKAGES)), \
             patch.object(model,'_sha256',side_effect=AssertionError('readiness must not read checkpoint bytes')):
            status=model.vjepa2_readiness()
        self.assertTrue(status['available'])
        self.assertTrue(status['checkpoint_hash_configured'])
        self.assertFalse(status['hash_source_verified'])
        self.assertTrue(status['local_pin_registered'])
        self.assertEqual(status['provider_authentication'],'unverified')
        self.assertIn('提供元の真正性は署名/公式checksumで検証できておらず',status['message'])
    def test_config_fingerprint_separates_resume_and_search(self):
        first=index_video(self.video,self.store,reserve_bytes=0)
        original_pipeline=first['pipeline']
        with patch('jepasearch.core.PREPROCESS_VERSION','rgb-grid-test-v2'):
            self.assertEqual(search(self.video,0,2,self.store),[])
            changed=index_video(self.video,self.store,reserve_bytes=0)
            self.assertEqual(changed['windows_added'],9)
            self.assertNotEqual(changed['pipeline'],original_pipeline)
            self.assertTrue(search(self.video,0,2,self.store))
            decide(self.store,changed['media_id'],0,'keep',pipeline=original_pipeline)
        self.assertTrue(search(self.video,0,2,self.store))
    def test_same_source_two_engines_remain_searchable_and_decisions_are_separate(self):
        baseline=index_video(self.video,self.store,reserve_bytes=0,engine='color-motion')
        fake_vector=__import__('numpy').ones(1024,dtype=__import__('numpy').float32)
        with patch('jepasearch.model.load_raw_vjepa2',return_value=(object(),object(),'cpu','mock-vjepa-model')):
            with patch('jepasearch.model.extract_features',return_value=fake_vector):
                jepa=index_video(self.video,self.store,reserve_bytes=0,engine='vjepa2')
                jepa_rows=search(self.video,0,2,self.store,engine='vjepa2',limit=5)
        baseline_rows=search(self.video,0,2,self.store,engine='color-motion',limit=5)
        self.assertTrue(baseline_rows)
        self.assertTrue(jepa_rows)
        self.assertTrue(all(row['pipeline']==baseline['pipeline'] for row in baseline_rows))
        self.assertTrue(all(row['pipeline']==jepa['pipeline'] for row in jepa_rows))
        decide(self.store,baseline['media_id'],0,'keep',engine='color-motion',pipeline=baseline['pipeline'])
        decide(self.store,jepa['media_id'],0,'hold',engine='vjepa2',pipeline=jepa['pipeline'])
        decisions={(row['pipeline'],row['decision']) for row in references(self.store)}
        self.assertEqual(decisions,{(baseline['pipeline'],'keep'),(jepa['pipeline'],'hold')})

    def test_legacy_schema_migration_preserves_rows_and_marks_unverifiable_pipeline_resumable(self):
        legacy_store=self.root/'legacy-index.sqlite'
        media_id='legacy-media-id'
        current_pipeline='color-motion-legacy-current'
        older_pipeline='vjepa2-legacy-older'
        legacy=__import__('sqlite3').connect(legacy_store)
        legacy.executescript("""
            CREATE TABLE media(media_id TEXT PRIMARY KEY,path TEXT NOT NULL,size INTEGER NOT NULL,
                mtime_ns INTEGER NOT NULL,duration_s REAL NOT NULL,fps REAL NOT NULL,
                width INTEGER NOT NULL,height INTEGER NOT NULL,pipeline TEXT NOT NULL,status TEXT NOT NULL);
            CREATE TABLE windows(media_id TEXT NOT NULL,pipeline TEXT NOT NULL,model_version TEXT NOT NULL,
                preprocess_version TEXT NOT NULL,window_version TEXT NOT NULL,start_s REAL NOT NULL,
                end_s REAL NOT NULL,feature BLOB NOT NULL,
                PRIMARY KEY(media_id,pipeline,model_version,preprocess_version,window_version,start_s));
            CREATE TABLE decisions(media_id TEXT NOT NULL,pipeline TEXT NOT NULL,start_s REAL NOT NULL,
                decision TEXT NOT NULL,PRIMARY KEY(media_id,pipeline,start_s));
        """)
        legacy.execute("INSERT INTO media VALUES(?,?,?,?,?,?,?,?,?,?)",
            (media_id,str(self.video),self.video.stat().st_size,self.video.stat().st_mtime_ns,6.0,30.0,320,180,current_pipeline,'complete'))
        windows=[(media_id,pipeline,'model-v1','prep-v1','window-v1',0.0,2.0,b'feature') for pipeline in (current_pipeline,older_pipeline)]
        legacy.executemany("INSERT INTO windows VALUES(?,?,?,?,?,?,?,?)",windows)
        legacy.executemany("INSERT INTO decisions VALUES(?,?,?,?)",[(media_id,pipeline,0.0,decision) for pipeline,decision in ((current_pipeline,'keep'),(older_pipeline,'hold'))])
        legacy.commit()
        legacy.close()
        migrated=connect(legacy_store)
        self.assertEqual(migrated.execute('PRAGMA user_version').fetchone()[0],2)
        self.assertEqual(migrated.execute('SELECT count(*) FROM media').fetchone()[0],1)
        self.assertEqual(migrated.execute('SELECT count(*) FROM windows').fetchone()[0],2)
        self.assertEqual(migrated.execute('SELECT count(*) FROM decisions').fetchone()[0],2)
        states=dict(migrated.execute('SELECT pipeline,status FROM media_pipeline_state WHERE media_id=?',(media_id,)).fetchall())
        self.assertEqual(states,{current_pipeline:'complete',older_pipeline:'interrupted'})
        migrated.close()
    def test_legacy_schema_search_matches_current_results_without_writing_migration(self):
        import sqlite3, stat
        index_video(self.video,self.store,reserve_bytes=0)
        current_rows=search(self.video,0,2,self.store,limit=5)
        legacy=connect(self.store)
        legacy.execute('DROP TABLE media_pipeline_state')
        legacy.execute('PRAGMA user_version=1')
        legacy.commit(); legacy.close()
        before=(hashlib.sha256(self.store.read_bytes()).digest(),self.store.stat().st_size,
                stat.S_IMODE(self.store.stat().st_mode))
        wal=Path(str(self.store)+'-wal')
        wal_before=(hashlib.sha256(wal.read_bytes()).digest(),wal.stat().st_size) if wal.exists() else None
        source_before=(hashlib.sha256(self.video.read_bytes()).digest(),self.video.stat().st_size,
                       stat.S_IMODE(self.video.stat().st_mode))
        legacy_rows=search(self.video,0,2,self.store,limit=5)
        after=(hashlib.sha256(self.store.read_bytes()).digest(),self.store.stat().st_size,
               stat.S_IMODE(self.store.stat().st_mode))
        wal_after=(hashlib.sha256(wal.read_bytes()).digest(),wal.stat().st_size) if wal.exists() else None
        source_after=(hashlib.sha256(self.video.read_bytes()).digest(),self.video.stat().st_size,
                      stat.S_IMODE(self.video.stat().st_mode))
        self.assertEqual(after,before)
        if wal_before is None:
            self.assertIn(wal_after,(None,(hashlib.sha256(b'').digest(),0)))
        else:
            self.assertEqual(wal_after,wal_before)
        self.assertEqual(source_after,source_before)
        self.assertEqual([(r['media_id'],r['start_s'],r['end_s']) for r in legacy_rows],
                         [(r['media_id'],r['start_s'],r['end_s']) for r in current_rows])
        readonly=sqlite3.connect(f'{self.store.as_uri()}?mode=ro',uri=True)
        self.assertEqual(readonly.execute('PRAGMA user_version').fetchone()[0],1)
        self.assertIsNone(readonly.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='media_pipeline_state'").fetchone())
        readonly.close()
        migrated=connect(self.store)
        self.assertEqual(migrated.execute('PRAGMA user_version').fetchone()[0],2)
        migrated.close()
        migrated_rows=search(self.video,0,2,self.store,limit=5)
        self.assertEqual([(r['media_id'],r['start_s'],r['end_s']) for r in migrated_rows],
                         [(r['media_id'],r['start_s'],r['end_s']) for r in current_rows])
    def test_search_reads_committed_uncheckpointed_wal_updates(self):
        import sqlite3
        indexed=index_video(self.video,self.store,reserve_bytes=0)
        source_before=(hashlib.sha256(self.video.read_bytes()).digest(),self.video.stat().st_size,
                       self.video.stat().st_mode)
        writer=sqlite3.connect(self.store,timeout=30)
        writer.execute('PRAGMA wal_autocheckpoint=0')
        writer.execute("UPDATE media_pipeline_state SET status='interrupted' WHERE media_id=? AND pipeline=?",
                       (indexed['media_id'],indexed['pipeline']))
        writer.commit()
        wal=Path(str(self.store)+'-wal')
        self.assertTrue(wal.exists())
        self.assertGreater(wal.stat().st_size,0)
        self.assertEqual(writer.execute('SELECT status FROM media_pipeline_state WHERE media_id=? AND pipeline=?',
                                        (indexed['media_id'],indexed['pipeline'])).fetchone()[0],'interrupted')
        rows=search(self.video,0,2,self.store)
        self.assertEqual(rows,[])
        source_after=(hashlib.sha256(self.video.read_bytes()).digest(),self.video.stat().st_size,
                      self.video.stat().st_mode)
        self.assertEqual(source_after,source_before)
        writer.close()
    def test_dangling_store_symlink_and_media_hardlink_are_refused(self):
        missing_target=self.root/'not-created.sqlite'
        dangling=self.root/'dangling.sqlite'
        dangling.symlink_to(missing_target)
        with self.assertRaisesRegex(SearchError,'must not be a symlink'):
            connect(dangling,self.video)
        self.assertFalse(missing_target.exists())
        alias=self.root/'media-hardlink.mp4'
        os.link(self.video,alias)
        with self.assertRaisesRegex(SearchError,'overwrite the source video'):
            connect(alias,self.video)
    def test_low_space_stops_before_database_creation(self):
        store=self.root/'low-space.sqlite'
        with self.assertRaisesRegex(SearchError,'before creating the store'):
            index_video(self.video,store,reserve_bytes=10**20)
        self.assertFalse(store.exists())
    def test_database_and_lock_files_are_owner_only(self):
        index_video(self.video,self.store,reserve_bytes=0)
        import stat
        for path in (self.store,Path(str(self.store)+'.lock'),Path(str(self.store)+'-wal'),Path(str(self.store)+'-shm')):
            if path.exists():
                self.assertEqual(stat.S_IMODE(path.stat().st_mode),0o600)
    def test_directory_at_database_path_is_refused_without_permission_change(self):
        import stat
        directory=self.root/'not-a-database.sqlite'
        directory.mkdir(mode=0o755)
        before=stat.S_IMODE(directory.stat().st_mode)
        with self.assertRaisesRegex(SearchError,'regular files'):
            connect(directory)
        self.assertEqual(stat.S_IMODE(directory.stat().st_mode),before)
    def test_search_has_bounded_result_and_feature_window_limits(self):
        from jepasearch import core
        with patch('jepasearch.core._query_feature') as query:
            with self.assertRaisesRegex(SearchError,'limit must be between'):
                search(self.video,0,2,self.store,limit=core.MAX_SEARCH_RESULTS+1)
            query.assert_not_called()
        index_video(self.video,self.store,reserve_bytes=0)
        with patch('jepasearch.core.MAX_SEARCH_INDEX_WINDOWS',1):
            with self.assertRaisesRegex(SearchError,'in-memory search window limit'):
                search(self.video,0,2,self.store)
    def test_oversized_synthetic_frame_dimensions_stop_before_database_creation(self):
        import cv2
        class OversizedSyntheticCapture:
            released=False
            def isOpened(self): return True
            def get(self, prop):
                return {cv2.CAP_PROP_FPS:30.0,cv2.CAP_PROP_FRAME_COUNT:60.0,
                        cv2.CAP_PROP_FRAME_WIDTH:7680.0,cv2.CAP_PROP_FRAME_HEIGHT:4320.0}.get(prop,0)
            def release(self): self.released=True
        capture=OversizedSyntheticCapture()
        store=self.root/'oversized.sqlite'
        with patch('jepasearch.core._open_local_video',return_value=capture):
            with self.assertRaisesRegex(SearchError,'frame-size limit'):
                index_video(self.video,store,reserve_bytes=0)
        self.assertTrue(capture.released)
        self.assertFalse(store.exists())
    def test_checkpoint_local_identity_pin_only_accepts_matching_synthetic_checkpoint(self):
        import hashlib, json
        from jepasearch.model import raw_model_version
        checkpoint=self.root/'synthetic-checkpoint.bin'
        checkpoint.write_bytes(b'synthetic checkpoint bytes')
        digest=hashlib.sha256(checkpoint.read_bytes()).hexdigest()
        identity=self.root/'identity.json'
        identity.write_text(json.dumps({
            'schema_version':1,'checkpoint_path':str(checkpoint),'sha256':digest,
            'source':{'description':'synthetic test fixture source'}
        }))
        with patch.dict(os.environ,{'JEPA_MODEL_IDENTITY_PATH':str(identity),'JEPA_RAW_CHECKPOINT_SHA256':'0'*64},clear=True):
            with patch('jepasearch.model._sha256') as read_checkpoint:
                with self.assertRaisesRegex(SearchError,'differs from the registered local pin'):
                    raw_model_version(str(checkpoint))
                read_checkpoint.assert_not_called()
        with patch.dict(os.environ,{'JEPA_MODEL_IDENTITY_PATH':str(identity),'JEPA_RAW_CHECKPOINT_SHA256':digest}):
            self.assertIn(digest[:16],raw_model_version(str(checkpoint)))
        with patch.dict(os.environ,{'JEPA_MODEL_IDENTITY_PATH':str(identity),'JEPA_RAW_CHECKPOINT_SHA256':'0'*64}):
            with self.assertRaisesRegex(SearchError,'differs from the registered local pin'):
                raw_model_version(str(checkpoint))

    def test_local_checkpoint_registration_records_identity_without_claiming_provider_authentication(self):
        import importlib.util, json, sys
        from unittest.mock import patch
        project=Path(__file__).resolve().parents[1]
        spec=importlib.util.spec_from_file_location('register_local_checkpoint',project/'scripts'/'register_local_checkpoint.py')
        module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        root=self.root/'registered-project'; root.mkdir()
        risk=root/'jepa-risk-detection'; (risk/'artifacts'/'checkpoints').mkdir(parents=True)
        checkpoint=risk/'artifacts'/'checkpoints'/'vitl.pt'; checkpoint.write_bytes(b'synthetic model bytes')
        module.PROJECT=root/'jepa-video-search'; module.PROJECT.mkdir()
        module.RISK_ROOT=risk; module.EXPECTED_CHECKPOINT=checkpoint; module.EXPECTED_SIZE=len(b'synthetic model bytes')
        args=['register_local_checkpoint.py','--checkpoint',str(checkpoint)]
        with patch.object(sys,'argv',args): self.assertEqual(module.main(),0)
        record=json.loads((module.PROJECT/'.local'/'checkpoint-identity.json').read_text())
        self.assertEqual(record['checkpoint_bytes'],len(b'synthetic model bytes'))
        self.assertFalse('verified' in record['source']['provider_checksum_status'] and 'unverified' not in record['source']['provider_checksum_status'])
        self.assertIn('not a provider authenticity proof',record['integrity_scope'])
    def test_loader_commit_and_source_digests_are_checked_with_synthetic_files(self):
        import hashlib
        from types import SimpleNamespace
        import jepasearch.model as model
        root=self.root/'loader'
        (root/'src'/'jepa_risk').mkdir(parents=True)
        files={'src/jepa_risk/predictor.py':b'synthetic predictor source',
               'src/jepa_risk/features.py':b'synthetic feature source'}
        for relative,content in files.items(): (root/relative).write_bytes(content)
        pins={relative:hashlib.sha256(content).hexdigest() for relative,content in files.items()}
        with patch.object(model,'PINNED_LOADER_COMMIT','synthetic-commit'):
            with patch.object(model,'PINNED_LOADER_FILES',pins):
                with patch('jepasearch.model.subprocess.run',side_effect=[
                    SimpleNamespace(stdout='synthetic-commit\n'),SimpleNamespace(stdout=''),
                    SimpleNamespace(stdout='synthetic-commit\n'),SimpleNamespace(stdout='')]):
                    model._verify_pinned_loader(root)
                    (root/'src'/'jepa_risk'/'predictor.py').write_bytes(b'changed')
                    with self.assertRaisesRegex(SearchError,'source does not match'):
                        model._verify_pinned_loader(root)
    def test_loader_refuses_dirty_source_tree(self):
        from types import SimpleNamespace
        import jepasearch.model as model
        with patch('jepasearch.model.subprocess.run',side_effect=[
            SimpleNamespace(stdout=model.PINNED_LOADER_COMMIT+'\n'),
            SimpleNamespace(stdout=' M src/jepa_risk/predictor.py\n')]):
            with self.assertRaisesRegex(SearchError,'source tree has unpinned changes'):
                model._verify_pinned_loader(self.root)
    def test_model_dependency_versions_must_match_the_declared_pins(self):
        from importlib.metadata import version
        import jepasearch.model as model
        with patch('importlib.metadata.version',side_effect=lambda name:model.PINNED_MODEL_PACKAGES[name]):
            model._verify_model_packages()
        with patch('importlib.metadata.version',side_effect=lambda _name:'0.0.0'):
            with self.assertRaisesRegex(SearchError,'do not match their pinned versions'):
                model._verify_model_packages()
    def test_terminal_seek_failure_retries_with_one_frame_tail_guard(self):
        import cv2
        from jepasearch.core import _sample
        class FinalSeekFails:
            def __init__(self, duration_s, fail_after_s): self.duration_s=duration_s; self.fail_after_s=fail_after_s; self.position=0.0
            def get(self, prop): return self.duration_s*30 if prop==cv2.CAP_PROP_FRAME_COUNT else 0
            def set(self, prop, value): self.position=value/1000; return True
            def read(self):
                if self.position>=self.fail_after_s: return False,None
                return True,np.zeros((8,8,3),dtype=np.uint8)
        import numpy as np
        retries=[0]
        frames=_sample(FinalSeekFails(2.0,1.95),30.0,0.0,2.0,count=8,retry_counter=retries)
        self.assertEqual(len(frames),8)
        self.assertEqual(retries,[1])

    def test_nonterminal_seek_failure_is_not_hidden_by_tail_retry(self):
        import cv2
        import numpy as np
        from jepasearch.core import _sample
        class InteriorSeekFails:
            def __init__(self): self.position=0.0
            def get(self, prop): return 90 if prop==cv2.CAP_PROP_FRAME_COUNT else 0
            def set(self, prop, value): self.position=value/1000; return True
            def read(self):
                if self.position>=1.45: return False,None
                return True,np.zeros((8,8,3),dtype=np.uint8)
        retries=[0]
        frames=_sample(InteriorSeekFails(),30.0,0.0,1.5,count=8,retry_counter=retries)
        self.assertEqual(frames,[])
        self.assertEqual(retries,[0])

    def test_vjepa_query_uses_tail_retry_window_fingerprint(self):
        import numpy as np
        from jepasearch.core import _query_feature
        with patch('jepasearch.model.load_raw_vjepa2',return_value=(object(),object(),'mps','test-model')):
            with patch('jepasearch.model.extract_features',return_value=np.ones(1024,dtype=np.float32)):
                result=_query_feature(self.video,0,2,engine='vjepa2')
        self.assertEqual(result[4],'2s-stride-0.5s-fpc64-terminal-2frame-retry-v2')

    def test_query_duration_must_match_index_window_before_decoder(self):
        for end in (1.5,3.0,2.0000011):
            with patch('jepasearch.core._open_local_video') as decoder:
                with self.assertRaisesRegex(SearchError,'must match indexed window length 2s within 1e-06s'):
                    search(self.video,0,end,self.store)
                decoder.assert_not_called()
    def test_query_duration_accepts_defined_float_tolerance(self):
        index_video(self.video,self.store,reserve_bytes=0)
        rows=search(self.video,0,2.0000005,self.store)
        self.assertTrue(rows)
    def test_cancelled_index_resumes_without_duplicate_windows(self):
        import threading
        cancel=threading.Event(); progress=[]
        def on_progress(event):
            progress.append(event)
            if event.get('stage')=='indexing' and event.get('done')==1:
                cancel.set()
        stopped=index_video(self.video,self.store,reserve_bytes=0,cancel_event=cancel,progress_callback=on_progress)
        self.assertEqual(stopped['status'],'cancelled')
        self.assertEqual(stopped['windows_added'],1)
        self.assertTrue(any(event.get('stage')=='interrupted' for event in progress))
        resumed=index_video(self.video,self.store,reserve_bytes=0)
        self.assertEqual(resumed['status'],'complete')
        self.assertEqual(resumed['windows_added'],8)

if __name__=='__main__': unittest.main()
