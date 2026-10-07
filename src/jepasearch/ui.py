from __future__ import annotations

import argparse
import http.cookies
import json
import mimetypes
import os
import re
import secrets
import subprocess
import sys
import threading
import time
import webbrowser
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

from .core import QUERY_DURATION_TOLERANCE_S, SearchError, decide, index_video, references, resolve_local_media, search

_FILE_ID = re.compile(r"^[0-9a-f]{32}$")
_CANDIDATE_ID = re.compile(r"^[0-9a-f]{32}$")
_ASSETS = {"/assets/app.css": "app.css", "/assets/app.js": "app.js"}
_MAX_BODY = 32 * 1024


class UIError(RuntimeError):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


@dataclass
class LocalMedia:
    file_id: str
    path: Path
    name: str
    size: int
    mtime_ns: int
    device: int
    inode: int
    media_id: str | None = None
    indexed_pipelines_by_store: dict[str, dict[str, str]] = field(default_factory=dict)


@dataclass
class UISession:
    token: str
    csrf: str
    store_dir: Path
    files: dict[str, LocalMedia] = field(default_factory=dict)
    indexed_media: dict[str, str] = field(default_factory=dict)
    candidates: dict[str, dict] = field(default_factory=dict)


def native_picker(kind: str) -> list[Path]:
    """Use a native macOS chooser; the selected path stays in this process."""
    if sys.platform != "darwin":
        raise UIError(501, "macOSのネイティブ選択ダイアログが必要です")
    if kind == "file":
        script = 'POSIX path of (choose file with prompt "動画を選択")'
    elif kind == "folder":
        script = 'POSIX path of (choose folder with prompt "索引の保存先フォルダを選択")'
    else:
        raise UIError(400, "選択の種類が不正です")
    try:
        result = subprocess.run(["/usr/bin/osascript", "-e", script], capture_output=True, text=True, timeout=300)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise UIError(503, "macOSのファイル選択を開始できませんでした") from exc
    if result.returncode != 0:
        if "-128" in result.stderr:
            return []
        raise UIError(503, "ファイル選択を完了できませんでした")
    value = result.stdout.rstrip("\r\n")
    return [Path(value)] if value else []


class LocalUIService:
    def __init__(self, *, picker: Callable[[str], list[Path]] = native_picker,
                 index_function=index_video, search_function=search,
                 decide_function=decide, references_function=references):
        self.picker = picker
        self.index_function = index_function
        self.search_function = search_function
        self.decide_function = decide_function
        self.references_function = references_function
        self.sessions: dict[str, UISession] = {}
        self.session_lock = threading.RLock()
        self.job_lock = threading.RLock()
        self.job: dict | None = None
        self.job_thread: threading.Thread | None = None
        self.cancel_event: threading.Event | None = None

    @staticmethod
    def default_store_dir() -> Path:
        return Path.home() / "Library" / "Application Support" / "jepa-video-search"

    def new_session(self) -> UISession:
        session = UISession(secrets.token_urlsafe(32), secrets.token_urlsafe(32), self.default_store_dir())
        with self.session_lock:
            self.sessions[session.token] = session
        return session

    def get_session(self, token: str | None) -> UISession | None:
        if not token:
            return None
        with self.session_lock:
            return self.sessions.get(token)

    @staticmethod
    def _safe_file(entry: LocalMedia) -> bool:
        try:
            path = resolve_local_media(entry.path)
            stat = path.stat()
            return path == entry.path and (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns) == (entry.device, entry.inode, entry.size, entry.mtime_ns)
        except (OSError, SearchError):
            return False

    def add_selected_files(self, session: UISession) -> list[dict]:
        added = []
        for value in self.picker("file"):
            try:
                path = resolve_local_media(value)
                stat = path.stat()
            except (OSError, SearchError):
                raise UIError(422, "選択したファイルは許可されたローカル動画として確認できませんでした")
            prior = next((item for item in session.files.values() if item.path == path), None)
            if prior:
                added.append(self._public_file(prior, session.store_dir))
                continue
            if len(session.files) >= 512:
                raise UIError(413, "一度に登録できる動画数の上限に達しました")
            item = LocalMedia(secrets.token_hex(16), path, path.name, stat.st_size, stat.st_mtime_ns, stat.st_dev, stat.st_ino)
            session.files[item.file_id] = item
            added.append(self._public_file(item, session.store_dir))
        return added

    @staticmethod
    def _public_file(item: LocalMedia, store_dir: Path) -> dict:
        pipelines = item.indexed_pipelines_by_store.get(str(store_dir), {})
        return {"id": item.file_id, "name": item.name, "size": item.size,
                "indexed": bool(pipelines),
                "indexed_pipelines": dict(pipelines)}

    def choose_store(self, session: UISession) -> str:
        paths = self.picker("folder")
        if not paths:
            return session.store_dir.name or "選択をキャンセルしました"
        try:
            path = paths[0].expanduser().resolve(strict=True)
            if not path.is_dir():
                raise ValueError("not a directory")
        except (OSError, ValueError):
            raise UIError(422, "保存先は存在するローカルフォルダを選択してください")
        session.store_dir = path
        return path.name or "選択したフォルダ"

    def store_path(self, session: UISession) -> Path:
        return session.store_dir / "index.sqlite"

    @staticmethod
    def _job_public(job: dict | None, *, owned: bool = True) -> dict | None:
        if not job:
            return None
        keys = ("id", "kind", "status", "stage", "done", "total", "current_name", "device", "message", "error", "results")
        result = {key: job.get(key) for key in keys}
        result["engine"] = job.get("engine", job.get("fields", {}).get("engine"))
        result["fingerprint"] = job.get("fingerprint")
        result["owned"] = owned
        return result

    def job_state(self, session: UISession) -> dict | None:
        with self.job_lock:
            if self.job and self.job["owner"] == session.token:
                return self._job_public(self.job, owned=True)
            if self.job and self.job["status"] in {"queued", "running", "cancelling"}:
                return {"id": None, "kind": self.job["kind"], "status": "running", "stage": "busy_elsewhere",
                        "done": 0, "total": 0, "current_name": "", "device": "",
                        "message": "別の画面セッションでローカル処理中です。完了後、素材を選び直してください。",
                        "error": None, "results": [], "owned": False}
        return None

    def _start_job(self, session: UISession, kind: str, fields: dict) -> dict:
        with self.job_lock:
            if self.job and self.job["status"] in {"queued", "running", "cancelling"}:
                raise UIError(409, "別の処理が進行中です。完了または停止後に再開してください")
            job = {"id": secrets.token_hex(16), "owner": session.token, "kind": kind,
                   "status": "queued", "stage": "queued", "done": 0, "total": 0,
                   "current_name": "", "device": "", "message": "準備中", "error": None,
                   "results": [], "fields": fields, "started_at": time.time()}
            self.job = job
            self.cancel_event = threading.Event()
            target = self._run_index if kind == "index" else self._run_search
            self.job_thread = threading.Thread(target=target, args=(session, job, self.cancel_event), name=f"jepa-ui-{kind}", daemon=True)
            self.job_thread.start()
            return self._job_public(job, owned=True)

    def start_index(self, session: UISession, file_ids: list[str], engine: str) -> dict:
        if engine not in {"vjepa2", "color-motion"}:
            raise UIError(400, "検索方式が不正です")
        unique = list(dict.fromkeys(file_ids))
        if not unique or len(unique) > 512 or any(not isinstance(fid, str) or fid not in session.files for fid in unique):
            raise UIError(400, "この画面で選択した動画を指定してください")
        for fid in unique:
            if not self._safe_file(session.files[fid]):
                raise UIError(410, "選択済み動画が移動・変更・削除されました。もう一度選択してください")
        session.candidates.clear()
        return self._start_job(session, "index", {"file_ids": unique, "engine": engine})

    def start_search(self, session: UISession, file_id: str, start: float, engine: str) -> dict:
        if engine not in {"vjepa2", "color-motion"}:
            raise UIError(400, "検索方式が不正です")
        item = session.files.get(file_id)
        if item is None:
            raise UIError(404, "選択した動画IDが見つかりません")
        if not self._safe_file(item):
            raise UIError(410, "選択済み動画が移動・変更・削除されました。もう一度選択してください")
        indexed_pipelines = item.indexed_pipelines_by_store.get(str(session.store_dir), {})
        if engine not in indexed_pipelines:
            engine_label = "V-JEPA 2" if engine == "vjepa2" else "Color + Motion"
            raise UIError(409, f"{engine_label}ではこの動画が未索引です。「索引を作成 / 再開」を完了してから検索してください")
        if isinstance(start, bool):
            raise UIError(400, "見本の開始時刻が不正です")
        try:
            start = float(start)
        except (TypeError, ValueError):
            raise UIError(400, "見本の開始時刻が不正です")
        if not (start >= 0 and start < float("inf")):
            raise UIError(400, "見本の開始時刻は0以上の有限値にしてください")
        store = self.store_path(session)
        if store.is_symlink() or not store.is_file():
            raise UIError(409, "先に動画を索引し、利用できるローカル索引を作成してください")
        session.candidates.clear()
        return self._start_job(session, "search", {"file_id": file_id, "start": start, "engine": engine})

    def cancel_job(self, session: UISession) -> dict:
        with self.job_lock:
            if not self.job or self.job["owner"] != session.token or self.job["status"] not in {"queued", "running", "cancelling"}:
                raise UIError(409, "停止できる処理はありません")
            self.job["status"] = "cancelling"
            self.job["stage"] = "cancelling"
            self.job["message"] = "現在のwindowを終えた後に停止します"
            assert self.cancel_event is not None
            self.cancel_event.set()
            return self._job_public(self.job, owned=True)

    def _update_job(self, job: dict, **updates) -> None:
        with self.job_lock:
            if self.job is job:
                job.update(updates)

    @staticmethod
    def _safe_error(exc: Exception, session: UISession) -> str:
        if isinstance(exc, SearchError):
            message = str(exc)
            for item in session.files.values():
                message = message.replace(str(item.path), "[ローカル動画]")
            message = message.replace(str(session.store_dir), "[ローカル保存先]")
            return message[:300]
        return "ローカル処理に失敗しました。動画または保存先を確認してください。詳細はこの画面には表示しません。"

    def _run_index(self, session: UISession, job: dict, cancel: threading.Event) -> None:
        self._update_job(job, status="running", stage="starting", message="索引処理を準備しています")
        try:
            for file_id in job["fields"]["file_ids"]:
                if cancel.is_set():
                    break
                item = session.files[file_id]
                def progress(event):
                    stage = event.get("stage", "indexing")
                    message = "V-JEPA2をローカルで起動中" if stage == "loading_model" else "窓を解析・保存中"
                    self._update_job(job, stage=stage, done=event.get("done", 0), total=event.get("total", 0),
                                     current_name=item.name, device=event.get("device", ""), message=message)
                result = self.index_function(item.path, self.store_path(session), engine=job["fields"]["engine"],
                                             progress_callback=progress, cancel_event=cancel)
                if result.get("media_id"):
                    item.media_id = result["media_id"]
                    session.indexed_media[result["media_id"]] = file_id
                if result.get("status") == "cancelled":
                    break
                if result.get("status") != "complete" or not result.get("pipeline"):
                    raise SearchError("index did not report a complete fingerprint")
                engine = job["fields"]["engine"]
                store_key = str(session.store_dir)
                item.indexed_pipelines_by_store.setdefault(store_key, {})[engine] = result["pipeline"]
                self._update_job(job, engine=engine, fingerprint=result["pipeline"])
            status = "cancelled" if cancel.is_set() else "complete"
            self._update_job(job, status=status, stage=status, message="索引を停止しました。再開で続きから処理できます" if status == "cancelled" else "索引が完了しました")
        except Exception as exc:
            self._update_job(job, status="failed", stage="failed", error=self._safe_error(exc, session), message="索引に失敗しました")

    def _run_search(self, session: UISession, job: dict, cancel: threading.Event) -> None:
        self._update_job(job, status="running", stage="searching", message="モデル起動と2秒区間の検索を実行中")
        try:
            fields = job["fields"]
            item = session.files[fields["file_id"]]
            matches = self.search_function(item.path, fields["start"], fields["start"] + 2.0,
                                           self.store_path(session), engine=fields["engine"])
            if cancel.is_set():
                self._update_job(job, status="cancelled", stage="cancelled", message="検索を停止しました。結果は破棄しました")
                return
            results = []
            for match in matches:
                candidate_id = secrets.token_hex(16)
                match_path = Path(match["path"]).expanduser()
                candidate_file_id = next((fid for fid, candidate in session.files.items()
                                          if candidate.media_id == match["media_id"] and candidate.path == match_path and self._safe_file(candidate)), None)
                entry = {"id": candidate_id, "name": session.files[candidate_file_id].name if candidate_file_id else "この画面で選択されていない候補",
                         "file_id": candidate_file_id, "media_id": match["media_id"], "start_s": match["start_s"],
                         "end_s": match["end_s"], "score": match["score"], "pipeline": match["pipeline"],
                         "engine": match["engine"], "fingerprint": match["pipeline"],
                         "device": match["device"], "preview_available": candidate_file_id is not None, "decision": None}
                session.candidates[candidate_id] = entry
                results.append({k: v for k, v in entry.items() if k != "media_id" and k != "pipeline"})
            fingerprint = results[0]["fingerprint"] if results else None
            self._update_job(job, status="complete", stage="complete", message="検索が完了しました", results=results,
                             engine=fields["engine"], fingerprint=fingerprint)
        except Exception as exc:
            self._update_job(job, status="failed", stage="failed", error=self._safe_error(exc, session), message="検索に失敗しました")

    def decide_candidate(self, session: UISession, candidate_id: str, decision: str) -> None:
        if not _CANDIDATE_ID.fullmatch(candidate_id) or decision not in {"keep", "hold"}:
            raise UIError(400, "候補または採否が不正です")
        candidate = session.candidates.get(candidate_id)
        if not candidate:
            raise UIError(404, "この検索セッションの候補が見つかりません")
        self.decide_function(self.store_path(session), candidate["media_id"], candidate["start_s"], decision,
                             engine="vjepa2" if candidate["pipeline"].startswith("vjepa2-") else "color-motion",
                             pipeline=candidate["pipeline"])
        candidate["decision"] = decision

    def state(self, session: UISession) -> dict:
        store = self.store_path(session)
        with self.job_lock:
            job = self.job_state(session)
        return {"files": [self._public_file(item, session.store_dir) for item in session.files.values()],
                "store_label": session.store_dir.name or "Application Support",
                "store_ready": store.is_file() and not store.is_symlink(),
                "job": job, "query_window_seconds": 2,
                "query_tolerance_seconds": QUERY_DURATION_TOLERANCE_S}

    def local_references(self, session: UISession) -> list[dict]:
        store = self.store_path(session)
        if store.is_symlink() or not store.is_file():
            return []
        rows = self.references_function(store)
        result = []
        for row in rows:
            path = Path(row["path"]).expanduser()
            file_id = next((fid for fid, item in session.files.items() if item.path == path and self._safe_file(item)), None)
            result.append({"name": session.files[file_id].name if file_id else "選択されていない動画",
                           "file_id": file_id, "media_id": row["media_id"], "start_s": row["start_s"],
                           "end_s": row["end_s"], "decision": row["decision"], "pipeline": row["pipeline"]})
        return result


class LoopbackHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, service: LocalUIService):
        self.service = service
        super().__init__(address, UIRequestHandler)


class UIRequestHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "JEPA-LocalUI"
    sys_version = ""

    def log_message(self, fmt, *args):
        # Never write request lines, selected file IDs, or paths to a log stream.
        return

    def _expected_origin(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}"

    def _host_ok(self) -> bool:
        return self.client_address[0] == "127.0.0.1" and self.headers.get("Host", "") == f"127.0.0.1:{self.server.server_port}"

    def _cookies(self) -> http.cookies.SimpleCookie:
        cookie = http.cookies.SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
        except http.cookies.CookieError:
            pass
        return cookie

    def _session(self, *, create: bool = False) -> UISession | None:
        cookie = self._cookies().get("jepa_session")
        session = self.server.service.get_session(cookie.value if cookie else None)
        if session is None and create:
            session = self.server.service.new_session()
        return session

    def _headers(self, *, content_type: str = "application/json; charset=utf-8", extra: dict | None = None) -> None:
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; media-src 'self'; connect-src 'self'; img-src 'self' data:; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
        if extra:
            for key, value in extra.items():
                self.send_header(key, value)

    def _send(self, status: int, body: bytes, *, content_type: str = "application/json; charset=utf-8", extra: dict | None = None) -> None:
        self.send_response(status)
        self._headers(content_type=content_type, extra=extra)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, value, *, extra: dict | None = None) -> None:
        self._send(status, json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"), extra=extra)

    def _error(self, status: int, message: str) -> None:
        self._json(status, {"error": message})

    def _auth_get(self) -> UISession | None:
        if not self._host_ok():
            self._error(400, "不正なHostです")
            return None
        session = self._session()
        if session is None:
            self._error(401, "画面セッションがありません。ページを再読み込みしてください")
        return session

    def _auth_post(self) -> UISession | None:
        if not self._host_ok():
            self._error(400, "不正なHostです")
            return None
        if self.headers.get("Origin") != self._expected_origin() or self.headers.get("Sec-Fetch-Site", "same-origin") not in {"same-origin", "none"}:
            self._error(403, "同一画面からの操作だけ許可されています")
            return None
        session = self._session()
        if session is None:
            self._error(401, "画面セッションがありません。ページを再読み込みしてください")
            return None
        if not secrets.compare_digest(self.headers.get("X-CSRF-Token", ""), session.csrf):
            self._error(403, "画面操作トークンが不正です")
            return None
        return session

    def _body(self) -> dict:
        if self.headers.get_content_type() != "application/json":
            raise UIError(415, "JSONリクエストだけ受け付けます")
        try:
            size = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise UIError(400, "リクエストサイズが不正です")
        if size < 0 or size > _MAX_BODY:
            raise UIError(413, "リクエストが大きすぎます")
        try:
            value = json.loads(self.rfile.read(size).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise UIError(400, "JSONを読み取れません")
        if not isinstance(value, dict):
            raise UIError(400, "JSON objectが必要です")
        return value

    def _media(self, file_id: str, *, head: bool = False) -> None:
        session = self._auth_get()
        if session is None:
            return
        if not _FILE_ID.fullmatch(file_id):
            self._error(404, "動画が見つかりません")
            return
        item = session.files.get(file_id)
        if item is None:
            self._error(404, "この画面で選択した動画だけ再生できます")
            return
        if not self.server.service._safe_file(item):
            self._error(410, "選択後に動画が移動・変更・削除されました")
            return
        size = item.size
        start, end, status = 0, max(0, size - 1), 200
        range_header = self.headers.get("Range")
        if range_header:
            match = re.fullmatch(r"bytes=(\d*)-(\d*)", range_header.strip())
            if not match or size == 0:
                self._send(416, b"", extra={"Content-Range": f"bytes */{size}"})
                return
            left, right = match.groups()
            if left:
                start = int(left)
                end = int(right) if right else size - 1
            elif right:
                suffix = int(right)
                if suffix <= 0:
                    self._send(416, b"", extra={"Content-Range": f"bytes */{size}"})
                    return
                start = max(0, size - suffix)
                end = size - 1
            if start >= size or start > end or end >= size:
                self._send(416, b"", extra={"Content-Range": f"bytes */{size}"})
                return
            status = 206
        length = end - start + 1
        try:
            flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(item.path, flags)
            stat = os.fstat(fd)
            if (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns) != (item.device, item.inode, item.size, item.mtime_ns):
                os.close(fd)
                self._error(410, "選択後に動画が移動・変更・削除されました")
                return
            media_stream = os.fdopen(fd, "rb")
        except OSError:
            self._error(410, "選択後に動画が移動・変更・削除されました")
            return
        mime = {".mp4": "video/mp4", ".mov": "video/quicktime", ".m4v": "video/mp4", ".avi": "video/x-msvideo", ".mkv": "video/x-matroska", ".webm": "video/webm", ".ts": "video/mp2t", ".mts": "video/mp2t", ".m2ts": "video/mp2t", ".mpg": "video/mpeg", ".mpeg": "video/mpeg"}.get(item.path.suffix.casefold(), "application/octet-stream")
        extra = {"Accept-Ranges": "bytes", "Content-Disposition": "inline", "X-Content-Type-Options": "nosniff"}
        if status == 206:
            extra["Content-Range"] = f"bytes {start}-{end}/{size}"
        self.send_response(status)
        self._headers(content_type=mime, extra=extra)
        self.send_header("Content-Length", str(length))
        self.end_headers()
        if head:
            media_stream.close()
            return
        try:
            with media_stream as stream:
                stream.seek(start)
                remaining = length
                while remaining:
                    data = stream.read(min(128 * 1024, remaining))
                    if not data:
                        break
                    self.wfile.write(data)
                    remaining -= len(data)
        except (BrokenPipeError, ConnectionResetError, OSError):
            return

    def do_HEAD(self):
        if not self._host_ok():
            self._error(400, "不正なHostです")
            return
        parsed = urlsplit(self.path)
        if parsed.path.startswith("/media/") and not parsed.query:
            self._media(parsed.path[len("/media/"):], head=True)
        else:
            self._send(404, b"", content_type="text/plain; charset=utf-8")

    def do_GET(self):
        if not self._host_ok():
            self._error(400, "不正なHostです")
            return
        parsed = urlsplit(self.path)
        if parsed.path == "/":
            session = self._session(create=True)
            if session is None:
                self._error(500, "セッションを作れません")
                return
            asset = Path(__file__).resolve().parent / "ui_assets" / "index.html"
            body = asset.read_text(encoding="utf-8").replace("__CSRF_TOKEN__", session.csrf).encode("utf-8")
            self._send(200, body, content_type="text/html; charset=utf-8", extra={"Set-Cookie": f"jepa_session={session.token}; Path=/; HttpOnly; SameSite=Strict"})
            return
        if parsed.path in _ASSETS:
            asset = Path(__file__).resolve().parent / "ui_assets" / _ASSETS[parsed.path]
            if not asset.is_file():
                self._send(404, b"", content_type="text/plain; charset=utf-8")
                return
            content_type = "text/css; charset=utf-8" if asset.suffix == ".css" else "text/javascript; charset=utf-8"
            self._send(200, asset.read_bytes(), content_type=content_type)
            return
        if parsed.path.startswith("/media/") and not parsed.query:
            self._media(parsed.path[len("/media/"):])
            return
        session = self._auth_get()
        if session is None:
            return
        if parsed.path == "/api/state":
            self._json(200, self.server.service.state(session))
        elif parsed.path == "/api/job":
            self._json(200, {"job": self.server.service.job_state(session)})
        elif parsed.path == "/api/references":
            self._json(200, {"references": self.server.service.local_references(session)})
        elif parsed.path == "/api/references/export":
            store = self.server.service.store_path(session)
            if store.is_symlink() or not store.is_file():
                self._error(404, "参照リストがありません")
                return
            rows = self.server.service.references_function(store)
            data = json.dumps(rows, ensure_ascii=False, indent=2).encode("utf-8")
            self._send(200, data, content_type="application/json; charset=utf-8", extra={"Content-Disposition": "attachment; filename=references.json"})
        else:
            self._send(404, b"", content_type="text/plain; charset=utf-8")

    def do_POST(self):
        session = self._auth_post()
        if session is None:
            return
        parsed = urlsplit(self.path)
        if parsed.query:
            self._error(404, "パスが見つかりません")
            return
        try:
            body = self._body()
            service = self.server.service
            if parsed.path == "/api/select-video":
                if body:
                    raise UIError(400, "ファイルパスを送信しないでください")
                self._json(200, {"files": service.add_selected_files(session)})
            elif parsed.path == "/api/select-store":
                if body:
                    raise UIError(400, "フォルダパスを送信しないでください")
                self._json(200, {"store_label": service.choose_store(session)})
            elif parsed.path == "/api/index":
                if set(body) != {"file_ids", "engine"} or not isinstance(body["file_ids"], list):
                    raise UIError(400, "索引リクエストが不正です")
                self._json(202, {"job": service.start_index(session, body["file_ids"], body["engine"])})
            elif parsed.path == "/api/search":
                if set(body) != {"file_id", "start", "engine"}:
                    raise UIError(400, "検索リクエストが不正です")
                self._json(202, {"job": service.start_search(session, body["file_id"], body["start"], body["engine"])})
            elif parsed.path == "/api/job/cancel":
                if body:
                    raise UIError(400, "停止リクエストに項目は不要です")
                self._json(200, {"job": service.cancel_job(session)})
            elif parsed.path == "/api/decision":
                if set(body) != {"candidate_id", "decision"}:
                    raise UIError(400, "採否リクエストが不正です")
                service.decide_candidate(session, body["candidate_id"], body["decision"])
                self._json(200, {"saved": True})
            else:
                self._error(404, "APIが見つかりません")
        except UIError as exc:
            self._error(exc.status, str(exc))
        except Exception:
            self._error(500, "ローカル処理に失敗しました")


def make_server(port: int = 0, *, service: LocalUIService | None = None) -> LoopbackHTTPServer:
    if not (0 <= port <= 65535):
        raise ValueError("port must be between 0 and 65535")
    return LoopbackHTTPServer(("127.0.0.1", port), service or LocalUIService())


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="jepa-search-ui")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-open", action="store_true", help="do not open the default browser")
    args = parser.parse_args(argv)
    server = make_server(args.port)
    url = f"http://127.0.0.1:{server.server_port}/"
    print(f"JEPA local UI: {url}", flush=True)
    if not args.no_open:
        webbrowser.open(url)
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
