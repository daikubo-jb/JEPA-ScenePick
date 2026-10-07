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
        self.assertEqual(row,('none-baseline-v1','rgb-grid-4x4-motion-v1','2s-stride-0.5s-samples-8-v1'))
    def test_low_space_and_source_overwrite_refused(self):
        with self.assertRaisesRegex(SearchError,'insufficient free space'): index_video(self.video,self.store,reserve_bytes=10**20)
        with self.assertRaisesRegex(SearchError,'overwrite'): connect(self.video,self.video)
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
        self.assertEqual(config['optional-dependencies']['model'],['Pillow==11.2.1','torch==2.7.1','transformers==4.53.3'])
    def test_missing_model_stops_locally(self):
        with self.assertRaisesRegex(SearchError,'configured local V-JEPA2 raw checkpoint is unavailable'):
            load_raw_vjepa2(str(self.root/'missing-model'))
    def test_config_fingerprint_separates_resume_and_search(self):
        first=index_video(self.video,self.store,reserve_bytes=0)
        original_pipeline=first['pipeline']
        with patch('jepasearch.core.PREPROCESS_VERSION','rgb-grid-test-v2'):
            self.assertEqual(search(self.video,0,2,self.store),[])
            changed=index_video(self.video,self.store,reserve_bytes=0)
            self.assertEqual(changed['windows_added'],9)
            self.assertNotEqual(changed['pipeline'],original_pipeline)
            self.assertTrue(search(self.video,0,2,self.store))
            with self.assertRaisesRegex(SearchError,'same feature fingerprint'):
                decide(self.store,changed['media_id'],0,'keep',pipeline=original_pipeline)
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
