from __future__ import annotations

import http.client
import hashlib
import io
import json
import os
import re
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.cookies import SimpleCookie
from pathlib import Path
from unittest.mock import patch

from jepasearch.synthetic import make_video
from jepasearch.ui import LocalUIService, UIError, make_server
from jepasearch.core import SearchError, index_video, search as core_search


class FakePicker:
    def __init__(self, video: Path, folder: Path):
        self.video = video
        self.folder = folder
        self.calls = []

    def __call__(self, kind: str):
        self.calls.append(kind)
        return [self.video if kind == "file" else self.folder]


def unready_vjepa():
    return {
        "available": False,
        "message": "V-JEPA 2は利用不可です。公式SHA-256を設定してください。",
        "blockers": ["trusted_sha256_pin_missing"],
        "checkpoint_hash_configured": False,
        "hash_source_verified": False,
        "dependencies": {"torch": "not-installed"},
    }


class StartupConfigurationTests(unittest.TestCase):
    def test_managed_ui_uses_project_model_venv_and_default_local_pin_without_hash_env(self):
        project = Path(__file__).resolve().parents[1]
        launcher = (project / "scripts" / "manage_ui_server.sh").read_text(encoding="utf-8")
        readiness = (project / "scripts" / "check_vjepa_ui_startup.sh").read_text(encoding="utf-8")
        finder = (project / "scripts" / "open_ui_server.command").read_text(encoding="utf-8")
        self.assertIn('MODEL_PYTHON="$PROJECT_ROOT/.venv-vjepa/bin/python"', launcher)
        self.assertNotIn("jepa-risk-detection/.venv/bin/python", launcher)
        self.assertIn("check_vjepa_ui_startup.sh", launcher)
        self.assertIn("vjepa2_readiness()", readiness)
        self.assertIn("model_weights_loaded", readiness)
        self.assertNotIn("JEPA_RAW_CHECKPOINT_SHA256", launcher + readiness + finder)
        self.assertIn("sh scripts/manage_ui_server.sh start", finder)

    def test_server_launcher_rejects_malformed_ports_before_pid_cleanup(self):
        project = Path(__file__).resolve().parents[1]
        launcher = project / "scripts" / "manage_ui_server.sh"
        for action in ("start", "stop"):
            with self.subTest(action=action), tempfile.TemporaryDirectory(prefix="jepa-pid-boundary-") as tmp:
                base = Path(tmp)
                pid_dir = base / "run"
                pid_dir.mkdir()
                project_key = hashlib.sha256(str(project.resolve()).encode()).hexdigest()[:12]
                (pid_dir / f"jepa-video-search-ui-{project_key}-..").mkdir()
                sentinel = base / "sentinel.pid"
                sentinel.write_text("keep\n", encoding="utf-8")
                env = os.environ.copy()
                env.update({"TMPDIR": str(pid_dir), "JEPA_UI_PORT": "../../../sentinel"})
                result = subprocess.run(["sh", str(launcher), action], cwd=project, env=env,
                                        capture_output=True, text=True, timeout=5)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep\n")


class UITestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="jepa-ui-synthetic-")
        self.root = Path(self.temp.name)
        self.video = make_video(self.root / "generated-ui-fixture.mp4", duration=4)
        self.picker = FakePicker(self.video, self.root)
        self.service = LocalUIService(picker=self.picker, model_readiness_fn=unready_vjepa)
        # Never probe the user's default Application Support index from UI tests.
        self.service.default_store_dir = lambda: self.root
        self.server = make_server(0, service=self.service)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_port
        self.origin = f"http://127.0.0.1:{self.port}"
        self.cookie = ""
        self.csrf = ""

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temp.cleanup()

    def request(self, method, path, *, body=None, headers=None, host=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        request_headers = {"Host": host or f"127.0.0.1:{self.port}"}
        if self.cookie:
            request_headers["Cookie"] = self.cookie
        if method == "POST":
            request_headers.update({"Origin": self.origin, "X-CSRF-Token": self.csrf,
                                    "Content-Type": "application/json", "Sec-Fetch-Site": "same-origin"})
        if headers:
            request_headers.update(headers)
        payload = None if body is None else json.dumps(body).encode()
        connection.request(method, path, body=payload, headers=request_headers)
        response = connection.getresponse()
        data = response.read()
        result = response.status, response.headers, data
        connection.close()
        return result

    def bootstrap(self):
        status, headers, body = self.request("GET", "/", headers={"Sec-Fetch-Site": "none"})
        self.assertEqual(status, 200)
        cookie = SimpleCookie()
        cookie.load(headers["Set-Cookie"])
        self.cookie = f"jepa_session={cookie['jepa_session'].value}"
        match = re.search(rb'<meta name="csrf-token" content="([^"]+)"', body)
        self.assertIsNotNone(match)
        self.csrf = match.group(1).decode()
        return body.decode()

    def choose_video_and_store(self):
        status, _, payload = self.request("POST", "/api/select-video", body={})
        self.assertEqual(status, 200, payload)
        file_id = json.loads(payload)["files"][0]["id"]
        status, _, payload = self.request("POST", "/api/select-store", body={})
        self.assertEqual(status, 200, payload)
        return file_id

    def wait_job(self, wanted="complete", timeout=8):
        deadline = time.time() + timeout
        while time.time() < deadline:
            status, _, payload = self.request("GET", "/api/job")
            self.assertEqual(status, 200)
            job = json.loads(payload)["job"]
            if job and job["status"] == wanted:
                return job
            time.sleep(.05)
        self.fail(f"job did not reach {wanted}")


class EngineReadinessTests(UITestCase):
    def test_unready_vjepa_is_rejected_before_queue_but_baseline_needs_explicit_selection(self):
        self.bootstrap()
        file_id = self.choose_video_and_store()
        status, _, payload = self.request("GET", "/api/state")
        engines = json.loads(payload)["engines"]
        self.assertFalse(engines["vjepa2"]["available"])
        self.assertTrue(engines["color-motion"]["available"])

        called = []
        def fake_index(path, store, **kwargs):
            called.append(kwargs["engine"])
            return {"status": "complete", "media_id": "c" * 64,
                    "pipeline": "color-motion-test-pipeline", "windows_added": 1}
        self.service.index_function = fake_index
        status, _, payload = self.request("POST", "/api/index", body={"file_ids": [file_id], "engine": "vjepa2"})
        self.assertEqual(status, 409)
        self.assertIn("公式SHA-256", payload.decode())
        self.assertIsNone(self.service.job)
        self.assertEqual(called, [])

        # Baseline starts only after the request explicitly names that engine.
        status, _, payload = self.request("POST", "/api/index", body={"file_ids": [file_id], "engine": "color-motion"})
        self.assertEqual(status, 202, payload)
        job = self.wait_job()
        self.assertEqual(job["engine"], "color-motion")
        self.assertEqual(called, ["color-motion"])


class LocalUISecurityTests(UITestCase):
    def test_cross_site_page_bootstrap_does_not_allocate_sessions(self):
        self.assertEqual(self.service.sessions, {})
        status, _, _ = self.request("GET", "/", headers={"Sec-Fetch-Site": "cross-site"})
        self.assertEqual(status, 403)
        self.assertEqual(self.service.sessions, {})
        status, _, _ = self.request("GET", "/", headers={"Origin": "http://attacker.invalid"})
        self.assertEqual(status, 403)
        self.assertEqual(self.service.sessions, {})
        status, _, _ = self.request("GET", "/")
        self.assertEqual(status, 403)
        self.assertEqual(self.service.sessions, {})
        status, headers, _ = self.request("GET", "/", headers={"Sec-Fetch-Site": "none"})
        self.assertEqual(status, 200)
        self.assertIn("Set-Cookie", headers)
        self.assertEqual(len(self.service.sessions), 1)

    def test_failed_job_hides_synthetic_private_path_from_response_and_stdio(self):
        self.bootstrap()
        file_id = self.choose_video_and_store()
        private_marker = "SYNTHETIC-PRIVATE-PATH-DO-NOT-LEAK"
        fake_private_path = f"/Users/example/Movies/{private_marker}/game.mp4"

        def failing_index(path, store, **kwargs):
            raise SearchError(f"decoder failed for {path}; auxiliary={fake_private_path}; store={store}")

        self.service.index_function = failing_index
        captured_out, captured_err = io.StringIO(), io.StringIO()
        with patch.object(sys, "stdout", captured_out), patch.object(sys, "stderr", captured_err):
            status, _, payload = self.request("POST", "/api/index", body={"file_ids": [file_id], "engine": "color-motion"})
            self.assertEqual(status, 202, payload)
            job = self.wait_job("failed")
            status, _, payload = self.request("GET", "/api/job")
            self.assertEqual(status, 200, payload)

        surfaces = (payload.decode("utf-8"), job["error"], captured_out.getvalue(), captured_err.getvalue())
        for surface in surfaces:
            self.assertNotIn(private_marker, surface)
            self.assertNotIn(fake_private_path, surface)
            self.assertNotIn(str(self.video), surface)
            self.assertNotIn(str(self.root), surface)
        self.assertIn("詳細はこの画面には表示しません", job["error"])

    def test_server_bounds_http_workers_and_applies_socket_timeout(self):
        from jepasearch.ui import HTTP_REQUEST_TIMEOUT_S, MAX_HTTP_WORKERS
        self.assertEqual(self.server.request_queue_size, 16)
        self.assertEqual(self.server.worker_slots._initial_value, MAX_HTTP_WORKERS)
        with patch('jepasearch.ui.MAX_HTTP_WORKERS',1):
            bounded=make_server(0,service=LocalUIService(picker=self.picker))
            client=socket.create_connection(('127.0.0.1',bounded.server_port),timeout=2)
            accepted,_=bounded.get_request()
            self.assertEqual(accepted.gettimeout(),HTTP_REQUEST_TIMEOUT_S)
            bounded.shutdown_request(accepted)
            client.close()
            left,right=socket.socketpair()
            self.assertTrue(bounded.worker_slots.acquire(blocking=False))
            bounded.process_request(left,('127.0.0.1',0))
            self.assertEqual(left.fileno(),-1)
            bounded.worker_slots.release()
            right.close()
            bounded.server_close()

    def test_session_and_media_registration_have_explicit_caps(self):
        from jepasearch.ui import MAX_MEDIA_FILE_BYTES, MAX_SESSIONS
        service=LocalUIService(picker=self.picker)
        service.default_store_dir=lambda:self.root
        with patch('jepasearch.ui.MAX_SESSIONS',1):
            first=service.new_session()
            with self.assertRaisesRegex(UIError,'セッション数の上限'):
                service.new_session()
            first.created_monotonic-=60*60*13
            self.assertIsNone(service.get_session(first.token))
            second=service.new_session()
        self.assertNotEqual(first.token,second.token)
        with patch('jepasearch.ui.MAX_MEDIA_FILE_BYTES',self.video.stat().st_size-1):
            with self.assertRaisesRegex(UIError,'登録上限'):
                service.add_selected_files(second)
        self.assertEqual(second.files,{})

    def test_index_job_batch_has_a_separate_limit_from_session_capacity(self):
        from dataclasses import replace
        service=LocalUIService(picker=self.picker)
        session=service.new_session()
        selected=service.add_selected_files(session)[0]['id']
        other_id='f'*32
        session.files[other_id]=replace(session.files[selected],file_id=other_id)
        with patch('jepasearch.ui.MAX_INDEX_FILES_PER_JOB',1):
            with self.assertRaisesRegex(UIError,'選択した動画'):
                service.start_index(session,[selected,other_id],'color-motion')

    def test_chunked_post_body_is_refused_before_json_parse(self):
        self.bootstrap()
        status,_,payload=self.request('POST','/api/select-video',body=None,
                                      headers={'Transfer-Encoding':'chunked'})
        self.assertEqual(status,400,payload)
        self.assertIn('chunked',payload.decode())
        self.assertEqual(self.picker.calls,[])

    def test_loopback_static_page_csp_and_no_remote_assets(self):
        self.assertEqual(self.server.server_address[0], "127.0.0.1")
        html = self.bootstrap()
        self.assertNotIn("__CSRF_TOKEN__", html)
        self.assertNotIn("https://", html)
        for asset in ("/assets/app.js", "/assets/app.css"):
            status, headers, payload = self.request("GET", asset)
            self.assertEqual(status, 200)
            self.assertNotIn("https://", payload.decode())
            self.assertIn("default-src 'self'", headers["Content-Security-Policy"])
        status, _, script = self.request("GET", "/assets/app.js")
        self.assertEqual(status, 200)
        self.assertIn("result-engine-mismatch", script.decode())
        self.assertIn("provenance.fingerprint", script.decode())
        self.assertIn("searchSequence", script.decode())
        self.assertIn("pollSequence", script.decode())
        self.assertIn("pendingDecisions", script.decode())
        self.assertIn("ArrowLeft", script.decode())
        self.assertIn('$("#video-empty").hidden = Boolean(query.value);', script.decode())
        status, _, _ = self.request("GET", "/api/state", host="attacker.invalid")
        self.assertEqual(status, 400)

    def test_vjepa_readiness_copy_keeps_provenance_collapsed_and_blockers_visible(self):
        html = self.bootstrap()
        self.assertIn('<details id="model-details" class="model-details" hidden>', html)
        self.assertIn("モデル詳細・検証状態", html)
        self.assertIn('id="model-details-message"', html)
        status, _, script = self.request("GET", "/assets/app.js")
        self.assertEqual(status, 200)
        source = script.decode()
        self.assertIn('jepa.available ? " · 利用可能" : " · 利用不可"', source)
        self.assertIn('? "V-JEPA 2：利用可能"', source)
        self.assertIn('$("#model-details-message").textContent = jepaMessage;', source)
        self.assertIn('`${jepaMessage} 索引は開始できません。利用する場合はColor + Motionを明示的に選択してください。`', source)
        self.assertIn('V-JEPA 2は利用不可です: ${jepaMessage}', source)

    def test_post_requires_exact_origin_csrf_and_has_no_path_api(self):
        self.bootstrap()
        headers = {"Origin": "http://attacker.invalid"}
        status, _, _ = self.request("POST", "/api/select-video", body={}, headers=headers)
        self.assertEqual(status, 403)
        headers = {"X-CSRF-Token": "wrong"}
        status, _, _ = self.request("POST", "/api/select-video", body={}, headers=headers)
        self.assertEqual(status, 403)
        before = len(self.picker.calls)
        status, _, payload = self.request("POST", "/api/select-video", body={"path": "/private/unknown.mp4"})
        self.assertEqual(status, 400)
        self.assertNotIn(b"/private/unknown.mp4", payload)
        self.assertEqual(len(self.picker.calls), before)

    def test_opaque_media_id_range_path_traversal_and_session_boundary(self):
        self.bootstrap()
        file_id = self.choose_video_and_store()
        status, headers, payload = self.request("GET", f"/media/{file_id}", headers={"Range": "bytes=0-15"})
        self.assertEqual(status, 206)
        self.assertEqual(headers["Content-Range"], f"bytes 0-15/{self.video.stat().st_size}")
        self.assertEqual(payload[4:8], b"ftyp")
        status, _, _ = self.request("GET", f"/media/{file_id}", headers={"Range": "bytes=0-2,4-8"})
        self.assertEqual(status, 416)
        status, _, _ = self.request("GET", "/media/../etc/passwd")
        self.assertEqual(status, 404)
        status, _, _ = self.request("GET", f"/media/{'0'*32}")
        self.assertEqual(status, 404)
        old_cookie, old_csrf = self.cookie, self.csrf
        self.cookie, self.csrf = "", ""
        self.bootstrap()
        status, _, _ = self.request("GET", f"/media/{file_id}")
        self.assertEqual(status, 404)
        self.cookie, self.csrf = old_cookie, old_csrf

    def test_synthetic_index_search_compare_decision_and_reference_export(self):
        html = self.bootstrap()
        self.assertIn('class="workbench"', html)
        self.assertIn('class="viewer-panel"', html)
        self.assertIn('class="results-panel"', html)
        self.assertIn("前後5秒を見る", html)
        self.assertIn("正解確率ではありません", html)
        self.assertIn('id="query-playback-status"', html)
        self.assertIn('id="candidate-playback-status"', html)
        self.assertIn('id="engine-readiness"', html)
        status, _, js = self.request("GET", "/assets/app.js")
        self.assertEqual(status, 200)
        self.assertIn("queryFileIndexedForSelectedEngine", js.decode())
        self.assertIn('job?.kind === "index"', js.decode())
        self.assertIn("renderStoreReadiness(latest)", js.decode())
        self.assertIn("player.dataset.rangeStart = String(begin);", js.decode())
        self.assertIn("player.currentTime >= end - 0.05", js.decode())
        self.assertIn("MediaError.MEDIA_ERR_SRC_NOT_SUPPORTED", js.decode())
        status, _, payload = self.request("GET", "/api/state")
        initial_state = json.loads(payload)
        self.assertEqual(initial_state["files"], [])
        self.assertFalse(initial_state["store_ready"])
        self.assertTrue(initial_state["engines"]["color-motion"]["available"])
        self.assertFalse(initial_state["engines"]["vjepa2"]["available"])
        file_id = self.choose_video_and_store()
        status, _, payload = self.request("POST", "/api/index", body={"file_ids": [file_id], "engine": "vjepa2"})
        self.assertEqual(status, 409)
        self.assertIn("公式SHA-256", payload.decode())
        self.assertIsNone(self.service.job)
        status, _, payload = self.request("POST", "/api/search", body={"file_id": file_id, "start": 0, "engine": "color-motion"})
        self.assertEqual(status, 409)
        self.assertIn("Color + Motionではこの動画が未索引", payload.decode())
        status, _, payload = self.request("POST", "/api/index", body={"file_ids": [file_id], "engine": "color-motion"})
        self.assertEqual(status, 202, payload)
        index_job = self.wait_job()
        self.assertEqual(index_job["engine"], "color-motion")
        self.assertTrue(index_job["fingerprint"].startswith("color-motion-"))
        status, _, payload = self.request("GET", "/api/state")
        state = json.loads(payload)
        self.assertTrue(state["store_ready"])
        self.assertEqual(stat.S_IMODE((self.root/"index.sqlite").stat().st_mode),0o600)
        self.assertEqual(stat.S_IMODE(Path(str(self.root/"index.sqlite")+".lock").stat().st_mode),0o600)
        self.assertTrue(state["files"][0]["indexed"])
        self.assertEqual(state["files"][0]["indexed_pipelines"]["color-motion"], index_job["fingerprint"])
        self.assertIs(self.service.index_function, index_video)
        self.assertIs(self.service.search_function, core_search)
        # A browser reload keeps the cookie-bound server session and its completed index.
        html = self.bootstrap()
        self.assertIn('value="vjepa2"', html)
        status, _, payload = self.request("GET", "/api/state")
        reloaded_state = json.loads(payload)
        self.assertEqual(reloaded_state["files"][0]["id"], file_id)
        self.assertEqual(reloaded_state["files"][0]["indexed_pipelines"]["color-motion"], index_job["fingerprint"])
        # A changed engine without a matching pipeline is rejected before a job is queued.
        status, _, payload = self.request("POST", "/api/search", body={"file_id": file_id, "start": 0, "engine": "vjepa2"})
        self.assertEqual(status, 409)
        self.assertIn("公式SHA-256", payload.decode())
        session_token = self.cookie.split("=", 1)[1]
        self.assertEqual(self.service.job_state(self.service.get_session(session_token))["id"], index_job["id"])
        status, _, _ = self.request("POST", "/api/search", body={"file_id": file_id, "start": 0, "end": 3, "engine": "color-motion"})
        self.assertEqual(status, 400)
        status, _, payload = self.request("POST", "/api/search", body={"file_id": file_id, "start": 0, "engine": "color-motion"})
        self.assertEqual(status, 202, payload)
        job = self.wait_job()
        self.assertTrue(job["results"])
        self.assertEqual(job["engine"], "color-motion")
        self.assertTrue(job["fingerprint"].startswith("color-motion-"))
        candidate = job["results"][0]
        self.assertTrue(candidate["preview_available"])
        self.assertEqual(candidate["engine"], "color-motion")
        self.assertEqual(candidate["fingerprint"], job["fingerprint"])
        self.assertEqual((candidate["start_s"], candidate["end_s"]), (0.0, 2.0))
        status, _, payload = self.request("POST", "/api/decision", body={"candidate_id": candidate["id"], "decision": "keep"})
        self.assertEqual(status, 200, payload)
        status, _, payload = self.request("GET", "/api/references")
        refs = json.loads(payload)["references"]
        self.assertEqual(refs[0]["decision"], "keep")
        self.assertEqual(refs[0]["name"], self.video.name)
        self.assertNotIn(str(self.root), payload.decode())
        status, _, _ = self.request("POST", "/api/search", body={"file_id": file_id, "start": 0, "engine": "color-motion"})
        self.assertEqual(status, 202)
        status, _, payload = self.request("POST", "/api/decision", body={"candidate_id": candidate["id"], "decision": "hold"})
        self.assertEqual(status, 404)
        status, headers, payload = self.request("GET", "/api/references/export")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Disposition"], "attachment; filename=references.json")
        self.assertIn(str(self.root), payload.decode())

    def test_switching_store_preserves_each_store_pipeline_metadata(self):
        self.bootstrap()
        file_id = self.choose_video_and_store()
        status, _, payload = self.request("POST", "/api/index", body={"file_ids": [file_id], "engine": "color-motion"})
        self.assertEqual(status, 202, payload)
        indexed = self.wait_job()
        first_store = self.root / "index.sqlite"
        self.assertTrue(first_store.is_file())

        alternate = self.root / "alternate-store"
        alternate.mkdir()
        self.picker.folder = alternate
        status, _, payload = self.request("POST", "/api/select-store", body={})
        self.assertEqual(status, 200, payload)
        status, _, payload = self.request("GET", "/api/state")
        alternate_state = json.loads(payload)
        self.assertFalse(alternate_state["files"][0]["indexed"])
        self.assertEqual(alternate_state["files"][0]["indexed_pipelines"], {})
        self.assertTrue(first_store.is_file())

        self.picker.folder = self.root
        status, _, payload = self.request("POST", "/api/select-store", body={})
        self.assertEqual(status, 200, payload)
        status, _, payload = self.request("GET", "/api/state")
        restored = json.loads(payload)
        self.assertTrue(restored["files"][0]["indexed"])
        self.assertEqual(restored["files"][0]["indexed_pipelines"]["color-motion"], indexed["fingerprint"])
        self.assertTrue(first_store.is_file())

    def test_failed_index_job_is_not_reported_as_indexed_or_searchable(self):
        self.bootstrap()
        file_id = self.choose_video_and_store()

        def broken_index(_path, _store, **_kwargs):
            return {"status": "failed", "media_id": "b" * 64}

        self.service.index_function = broken_index
        status, _, payload = self.request("POST", "/api/index", body={"file_ids": [file_id], "engine": "color-motion"})
        self.assertEqual(status, 202, payload)
        job = self.wait_job("failed")
        self.assertEqual(job["status"], "failed")
        self.assertTrue(job["error"])
        status, _, payload = self.request("GET", "/api/state")
        failed_state = json.loads(payload)
        self.assertFalse(failed_state["files"][0]["indexed"])
        self.assertEqual(failed_state["files"][0]["indexed_pipelines"], {})
        status, _, payload = self.request("POST", "/api/search", body={"file_id": file_id, "start": 0, "engine": "color-motion"})
        self.assertEqual(status, 409)
        self.assertIn("Color + Motionではこの動画が未索引", payload.decode())

    def test_missing_or_changed_source_cannot_be_played_or_indexed(self):
        self.bootstrap()
        file_id = self.choose_video_and_store()
        self.video.unlink()
        status, _, _ = self.request("GET", f"/media/{file_id}")
        self.assertEqual(status, 410)
        status, _, payload = self.request("POST", "/api/index", body={"file_ids": [file_id], "engine": "color-motion"})
        self.assertEqual(status, 410)


class UIJobLifecycleTests(unittest.TestCase):
    def test_duplicate_job_cancel_resume_and_session_reconnect(self):
        temp = tempfile.TemporaryDirectory(prefix="jepa-ui-job-synthetic-")
        root = Path(temp.name)
        video = make_video(root / "job-fixture.mp4", duration=3)
        picker = FakePicker(video, root)
        started = threading.Event()
        call_count = []

        def fake_index(path, store, *, progress_callback, cancel_event, **_kwargs):
            call_count.append(1)
            if len(call_count) == 1:
                progress_callback({"stage": "loading_model", "done": 0, "total": 0})
                started.set()
                cancel_event.wait(3)
                return {"status": "cancelled", "media_id": "a" * 64, "windows_added": 1}
            return {"status": "complete", "media_id": "a" * 64, "pipeline": "vjepa2-test-fingerprint", "windows_added": 2}

        service = LocalUIService(picker=picker, index_function=fake_index,
                                 model_readiness_fn=lambda: {"available": True, "message": "mock ready", "blockers": [], "checkpoint_hash_configured": True, "hash_source_verified": False, "dependencies": {}})
        service.default_store_dir = lambda: root
        owner = service.new_session()
        another = service.new_session()
        file_id = service.add_selected_files(owner)[0]["id"]
        first = service.start_index(owner, [file_id], "vjepa2")
        self.assertEqual(first["status"], "queued")
        self.assertTrue(started.wait(2))
        with self.assertRaisesRegex(UIError, "進行中"):
            service.start_index(owner, [file_id], "vjepa2")
        foreign = service.job_state(another)
        self.assertFalse(foreign["owned"])
        with self.assertRaisesRegex(UIError, "停止できる処理"):
            service.cancel_job(another)
        service.cancel_job(owner)
        deadline = time.time() + 3
        while time.time() < deadline:
            job = service.job_state(owner)
            if job["status"] == "cancelled":
                break
            time.sleep(.02)
        self.assertEqual(job["status"], "cancelled")
        resumed = service.start_index(owner, [file_id], "vjepa2")
        self.assertEqual(resumed["status"], "queued")
        deadline = time.time() + 3
        while time.time() < deadline:
            job = service.job_state(owner)
            if job["status"] == "complete":
                break
            time.sleep(.02)
        self.assertEqual(job["status"], "complete")
        self.assertEqual(len(call_count), 2)
        self.assertEqual(service.job_state(owner)["id"], resumed["id"])
        temp.cleanup()


if __name__ == "__main__":
    unittest.main()
