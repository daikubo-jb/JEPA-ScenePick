from __future__ import annotations
import fcntl, hashlib, json, math, os, re, shutil, sqlite3, stat, threading, time
from pathlib import Path
from typing import Callable
import cv2
import numpy as np

PIPELINE = "color-motion-v1"
MODEL_VERSION = "none-baseline-v1"
PREPROCESS_VERSION = "rgb-grid-4x4-motion-v1"
WINDOW_VERSION = "2s-stride-0.5s-samples-8-terminal-2frame-retry-v2"
WINDOW_S, STRIDE_S = 2.0, 0.5
QUERY_DURATION_TOLERANCE_S = 1e-6
MAX_VIDEO_DURATION_S = 12 * 60 * 60
MAX_FRAME_PIXELS = 3840 * 2160
MAX_SEARCH_INDEX_WINDOWS = 50_000
MAX_SEARCH_RESULTS = 500

class SearchError(RuntimeError): pass

_ALLOWED_MEDIA_SUFFIXES={".mp4",".mov",".m4v",".avi",".mkv",".webm",".mpg",".mpeg",".mts",".m2ts",".ts"}
_PLAYLIST_SUFFIXES={".m3u",".m3u8",".pls",".asx",".xspf",".strm",".url",".webloc"}
_DECODER_OPTIONS_LOCK = threading.Lock()

def _has_allowed_container_signature(path: Path) -> bool:
    """Only accept binary container signatures we intend to decode as local files."""
    try:
        with path.open("rb") as stream:
            header = stream.read(4096)
    except OSError as exc:
        raise SearchError("local video file cannot be read") from exc
    suffix = path.suffix.casefold()
    if suffix in {".mp4", ".mov", ".m4v"}:
        return len(header) >= 12 and header[4:8] == b"ftyp"
    if suffix == ".avi":
        return len(header) >= 12 and header[:4] == b"RIFF" and header[8:12] == b"AVI "
    if suffix in {".mkv", ".webm"}:
        return header.startswith(b"\x1aE\xdf\xa3")
    if suffix in {".mpg", ".mpeg"}:
        return header.startswith((b"\x00\x00\x01\xba", b"\x00\x00\x01\xbb"))
    if suffix in {".ts", ".mts", ".m2ts"}:
        # Transport streams commonly use 188-byte packets; M2TS uses 192-byte packets.
        return any(len(header) > offset + stride * 2 and header[offset] == header[offset + stride] == header[offset + stride * 2] == 0x47
                   for stride in (188, 192, 204) for offset in (0, 4))
    return False

def resolve_local_media(video: Path | str) -> Path:
    raw=os.fspath(video)
    if "\x00" in raw or re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:",raw) or raw.startswith(("//","\\")):
        raise SearchError("input must be an explicit local video file path")
    candidate=Path(raw).expanduser()
    if candidate.suffix.casefold() in _PLAYLIST_SUFFIXES or candidate.suffix.casefold() not in _ALLOWED_MEDIA_SUFFIXES:
        raise SearchError("URL, stream, playlist, and unsupported file inputs are refused")
    try: source=candidate.resolve(strict=True)
    except OSError as exc: raise SearchError("local video file does not exist or is not accessible") from exc
    if not source.is_file(): raise SearchError("input must be a regular local video file")
    if not _has_allowed_container_signature(source):
        raise SearchError("video container signature is not an allowed local binary format")
    return source

def _open_local_video(source: Path):
    """Force FFmpeg's local-file protocol while opening an already validated path."""
    with _DECODER_OPTIONS_LOCK:
        key = "OPENCV_FFMPEG_CAPTURE_OPTIONS"
        previous = os.environ.get(key)
        os.environ[key] = "protocol_whitelist;file"
        try:
            cap = cv2.VideoCapture(str(source), cv2.CAP_FFMPEG)
        finally:
            if previous is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = previous
    return cap

def _db_path(store: Path, source: Path | None = None) -> Path:
    store = store.expanduser().absolute()
    if store.is_symlink(): raise SearchError("store must not be a symlink")
    store.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    parent = store.parent.resolve(strict=True)
    resolved = parent / store.name
    if source is not None:
        src = source.expanduser().resolve()
        same_inode = False
        if resolved.exists():
            try: same_inode = os.path.samefile(resolved, src)
            except OSError: same_inode = False
        if resolved == src or same_inode:
            raise SearchError("store must not overwrite the source video")
    return resolved

def _restrict_file_permissions(path: Path, source: Path | None = None) -> None:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise SearchError("database and sidecar paths must be regular files without symlinks") from exc
    source_fd = None
    try:
        target_stat = os.fstat(fd)
        if not stat.S_ISREG(target_stat.st_mode):
            raise SearchError("database and sidecar paths must be regular files without symlinks")
        if source is not None:
            try:
                source_fd = os.open(source, flags)
            except OSError as exc:
                raise SearchError("source video could not be checked safely") from exc
            source_stat = os.fstat(source_fd)
            if (target_stat.st_dev, target_stat.st_ino) == (source_stat.st_dev, source_stat.st_ino):
                raise SearchError("store must not overwrite the source video")
        try:
            os.fchmod(fd, 0o600)
        except OSError as exc:
            raise SearchError("database permissions could not be restricted to the current user") from exc
    finally:
        if source_fd is not None:
            os.close(source_fd)
        os.close(fd)

def _create_private_file(path: Path, source: Path | None = None) -> None:
    flags = os.O_CREAT | os.O_EXCL | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags, 0o600)
    except FileExistsError:
        _restrict_file_permissions(path, source)
    except OSError as exc:
        raise SearchError("private database file could not be created safely") from exc
    else:
        try:
            if source is not None:
                source_fd = os.open(source, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
                try:
                    target_stat, source_stat = os.fstat(fd), os.fstat(source_fd)
                    if (target_stat.st_dev, target_stat.st_ino) == (source_stat.st_dev, source_stat.st_ino):
                        raise SearchError("store must not overwrite the source video")
                finally:
                    os.close(source_fd)
        finally:
            os.close(fd)

def connect(store: Path, source: Path | None = None) -> sqlite3.Connection:
    path = _db_path(store, source)
    _create_private_file(path, source)
    db = sqlite3.connect(path, timeout=30)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=FULL")
    for suffix in ("-wal", "-shm"):
        sidecar = Path(str(path) + suffix)
        if sidecar.exists() or sidecar.is_symlink():
            _restrict_file_permissions(sidecar)
    try:
        db.execute("BEGIN IMMEDIATE")
        schema_version = int(db.execute("PRAGMA user_version").fetchone()[0])
        if schema_version > 2:
            raise SearchError("index database schema is newer than this application")
        db.execute("""CREATE TABLE IF NOT EXISTS media(
            media_id TEXT PRIMARY KEY, path TEXT NOT NULL, size INTEGER NOT NULL,
            mtime_ns INTEGER NOT NULL, duration_s REAL NOT NULL, fps REAL NOT NULL,
            width INTEGER NOT NULL, height INTEGER NOT NULL, pipeline TEXT NOT NULL,
            status TEXT NOT NULL)""")
        db.execute("""CREATE TABLE IF NOT EXISTS windows(
            media_id TEXT NOT NULL, pipeline TEXT NOT NULL, model_version TEXT NOT NULL,
            preprocess_version TEXT NOT NULL, window_version TEXT NOT NULL,
            start_s REAL NOT NULL, end_s REAL NOT NULL, feature BLOB NOT NULL,
            PRIMARY KEY(media_id,pipeline,model_version,preprocess_version,window_version,start_s))""")
        db.execute("""CREATE TABLE IF NOT EXISTS decisions(
            media_id TEXT NOT NULL, pipeline TEXT NOT NULL, start_s REAL NOT NULL,
            decision TEXT NOT NULL, PRIMARY KEY(media_id,pipeline,start_s))""")
        db.execute("""CREATE TABLE IF NOT EXISTS media_pipeline_state(
            media_id TEXT NOT NULL, pipeline TEXT NOT NULL, status TEXT NOT NULL,
            PRIMARY KEY(media_id,pipeline))""")
        # Preserve the legacy media row's authoritative status. Other fingerprints
        # had no status column; keep their windows and require resume to establish
        # completeness instead of promoting possibly partial data as searchable.
        db.execute("""INSERT OR IGNORE INTO media_pipeline_state(media_id,pipeline,status)
            SELECT media_id,pipeline,status FROM media""")
        db.execute("""INSERT OR IGNORE INTO media_pipeline_state(media_id,pipeline,status)
            SELECT DISTINCT w.media_id,w.pipeline,'interrupted'
            FROM windows AS w JOIN media AS m USING(media_id)
            WHERE w.pipeline != m.pipeline""")
        db.execute("PRAGMA user_version=2")
        db.commit()
        for suffix in ("-wal", "-shm"):
            sidecar = Path(str(path) + suffix)
            if sidecar.exists() or sidecar.is_symlink():
                _restrict_file_permissions(sidecar)
    except Exception:
        db.rollback()
        db.close()
        raise
    return db

def connect_readonly(store: Path, source: Path | None = None) -> sqlite3.Connection:
    """Open an existing index read-only while honoring SQLite WAL snapshots."""
    path = _db_path(store, source)
    try:
        db = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=30)
        db.execute("PRAGMA query_only=ON")
    except sqlite3.Error as exc:
        raise SearchError("index database could not be opened read-only") from exc
    return db

def _digest(path: Path) -> str:
    h=hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda:f.read(1024*1024),b''): h.update(chunk)
    return h.hexdigest()

def _pipeline_id(engine: str, model_version: str, preprocess_version: str,
                 window_version: str, device: str) -> str:
    # The id binds every fixed input/feature choice. A change creates a separate
    # resume/search/decision namespace instead of silently mixing embeddings.
    spec = {
        "engine": engine,
        "feature_code": "feature-contract-v2",
        "model": model_version,
        "preprocess": preprocess_version,
        "window": window_version,
        "window_seconds": WINDOW_S,
        "stride_seconds": STRIDE_S,
        "sample_count": 64 if engine == "vjepa2" else 8,
        "storage_dtype": "float32",
        "device": device.casefold().replace(" ", "-"),
        "vjepa_pooling": "mean-token-32x16x16-v1" if engine == "vjepa2" else None,
    }
    encoded = json.dumps(spec, sort_keys=True, separators=(",", ":")).encode()
    return f"{engine}-{hashlib.sha256(encoded).hexdigest()[:20]}"

def _feature(frames: list[np.ndarray]) -> np.ndarray:
    # Four-by-four RGB spatial means per frame, then summarize motion; synthetic baseline only.
    tokens=[]; previous=None; motions=[]
    for bgr in frames:
        rgb=cv2.cvtColor(cv2.resize(bgr,(64,64)), cv2.COLOR_BGR2RGB).astype(np.float32)/255
        grid=rgb.reshape(4,16,4,16,3).mean(axis=(1,3)).reshape(-1)
        gray=cv2.cvtColor(bgr,cv2.COLOR_BGR2GRAY)
        if previous is not None: motions.append(float(cv2.absdiff(gray,previous).mean())/255)
        previous=gray
        tokens.extend(grid.tolist())
    vector=np.asarray(tokens,dtype=np.float32)
    vector=np.concatenate([vector, np.asarray([np.mean(motions) if motions else 0],dtype=np.float32)])
    norm=float(np.linalg.norm(vector))
    return vector/(norm or 1)

def _sample(cap, fps: float, start: float, end: float, count: int=8,
            retry_counter: list[int] | None=None) -> list[np.ndarray]:
    def sample_with_tail_margin(frame_margin: int) -> list[np.ndarray]:
        frames=[]
        endpoint=max(start,end-frame_margin/max(fps,1))
        for t in np.linspace(start,endpoint,count):
            cap.set(cv2.CAP_PROP_POS_MSEC,float(t)*1000)
            ok,frame=cap.read()
            if not ok: return frames
            frames.append(frame)
        return frames

    frames=sample_with_tail_margin(1)
    if len(frames)==count: return frames
    # Some decoders advertise and sequentially decode the last frame but cannot
    # seek to it exactly. Retry only the final half-second window, shifting the
    # endpoint back one extra frame; do not hide corruption in interior windows.
    frame_count=int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    duration=frame_count/max(fps,1)
    if frame_count>0 and end>=duration-STRIDE_S-1e-6:
        retried=sample_with_tail_margin(2)
        if len(retried)==count:
            if retry_counter is not None: retry_counter[0]+=1
            return retried
    return []

def index_video(video: Path, store: Path, *, resume: bool=True, reserve_bytes: int=256*1024*1024, engine: str="color-motion", checkpoint_path: str|None=None, progress_callback: Callable[[dict], None]|None=None, cancel_event: threading.Event|None=None) -> dict:
    def report(**event):
        if progress_callback is not None:
            try: progress_callback(event)
            except Exception: pass
    if engine not in {"color-motion","vjepa2"}: raise SearchError("engine must be color-motion or vjepa2")
    source=resolve_local_media(video)
    if engine=="vjepa2":
        report(stage="loading_model",done=0,total=0,video_name=source.name)
        from .model import load_raw_vjepa2, PROCESSOR_CONFIG_VERSION
        model,processor,model_device,model_version=load_raw_vjepa2(checkpoint_path)
        preprocess_version=PROCESSOR_CONFIG_VERSION; window_version="2s-stride-0.5s-fpc64-terminal-2frame-retry-v2"
    else:
        model=processor=None; model_device="OpenCV CPU"; model_version=MODEL_VERSION; preprocess_version=PREPROCESS_VERSION; window_version=WINDOW_VERSION
    pipeline=_pipeline_id(engine,model_version,preprocess_version,window_version,model_device)
    before=source.stat(); media_id=_digest(source)
    store_path=_db_path(store,source)
    cap=_open_local_video(source)
    if not cap.isOpened(): raise SearchError("cannot decode validated local video")
    fps=float(cap.get(cv2.CAP_PROP_FPS) or 0); n=int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    w=int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)); h=int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    if fps<=0 or n<=0: cap.release(); raise SearchError("invalid video timing metadata")
    duration=n/fps
    if duration > MAX_VIDEO_DURATION_S or w <= 0 or h <= 0 or w * h > MAX_FRAME_PIXELS:
        cap.release(); raise SearchError("video exceeds the supported duration or frame-size limit")
    starts=np.arange(0,max(0,duration-WINDOW_S)+1e-8,STRIDE_S)
    estimate=int(len(starts)*8192+65536)
    if shutil.disk_usage(store_path.parent).free < estimate+reserve_bytes:
        cap.release(); raise SearchError("insufficient free space; indexing stopped before creating the store")
    db=connect(store,source)
    lock_path=Path(str(store_path)+".lock")
    _create_private_file(lock_path)
    with lock_path.open('a') as lock:
      try: fcntl.flock(lock, fcntl.LOCK_EX|fcntl.LOCK_NB)
      except BlockingIOError as e: cap.release(); db.close(); raise SearchError("another indexing run is active") from e
      existing=db.execute("SELECT status FROM media_pipeline_state WHERE media_id=? AND pipeline=?",(media_id,pipeline)).fetchone()
      if existing and existing[0]=='complete' and not resume:
          cap.release(); db.close(); raise SearchError("already indexed; use resume or a new store")
      db.execute("""INSERT INTO media VALUES(?,?,?,?,?,?,?,?,?,?)
          ON CONFLICT(media_id) DO UPDATE SET path=excluded.path,size=excluded.size,
          mtime_ns=excluded.mtime_ns,duration_s=excluded.duration_s,fps=excluded.fps,
          width=excluded.width,height=excluded.height,pipeline=excluded.pipeline,status=excluded.status""",
          (media_id,str(source),before.st_size,before.st_mtime_ns,duration,fps,w,h,pipeline,'indexing'))
      db.execute("""INSERT INTO media_pipeline_state VALUES(?,?,?)
          ON CONFLICT(media_id,pipeline) DO UPDATE SET status=excluded.status""",
          (media_id,pipeline,'indexing'))
      db.commit()
      done=0; decode_s=0.0; feature_s=0.0; persist_s=0.0
      terminal_seek_retries=[0]
      processed=0
      report(stage="indexing",done=0,total=len(starts),video_name=source.name,device=model_device)
      cancelled=False
      try:
        for start in starts:
          if cancel_event is not None and cancel_event.is_set():
              db.execute("UPDATE media SET status='interrupted' WHERE media_id=? AND pipeline=?",(media_id,pipeline))
              db.execute("UPDATE media_pipeline_state SET status='interrupted' WHERE media_id=? AND pipeline=?",(media_id,pipeline)); db.commit()
              cancelled=True
              report(stage="interrupted",done=processed,total=len(starts),video_name=source.name,device=model_device)
              break
          exists=db.execute("SELECT 1 FROM windows WHERE media_id=? AND pipeline=? AND model_version=? AND preprocess_version=? AND window_version=? AND start_s=?",(media_id,pipeline,model_version,preprocess_version,window_version,float(start))).fetchone()
          if exists:
              processed+=1
              report(stage="indexing",done=processed,total=len(starts),video_name=source.name,device=model_device)
              continue
          phase_t=time.perf_counter(); frames=_sample(cap,fps,float(start),float(start)+WINDOW_S,count=64 if engine=="vjepa2" else 8,retry_counter=terminal_seek_retries); decode_s+=time.perf_counter()-phase_t
          if len(frames)!=(64 if engine=="vjepa2" else 8): raise SearchError("could not sample an expected window")
          phase_t=time.perf_counter()
          if engine=="vjepa2":
              from .model import extract_features
              rgb=[cv2.cvtColor(frame,cv2.COLOR_BGR2RGB) for frame in frames]; vector=extract_features(rgb,model,processor)
          else: vector=_feature(frames)
          feature_s+=time.perf_counter()-phase_t
          phase_t=time.perf_counter(); db.execute("INSERT OR IGNORE INTO windows VALUES(?,?,?,?,?,?,?,?)",(media_id,pipeline,model_version,preprocess_version,window_version,float(start),float(start)+WINDOW_S,np.asarray(vector,dtype=np.float32).tobytes())); db.commit(); persist_s+=time.perf_counter()-phase_t; done+=1
          processed+=1
          report(stage="indexing",done=processed,total=len(starts),video_name=source.name,device=model_device,current_start_s=round(float(start),3))
        if cancelled:
            return {"media_id":media_id,"windows_added":done,"duration_s":round(duration,3),"pipeline":pipeline,"model_version":model_version,"preprocess_version":preprocess_version,"window_version":window_version,"device":model_device,"decode_seconds":decode_s,"feature_seconds":feature_s,"persist_seconds":persist_s,"terminal_seek_retry_windows":terminal_seek_retries[0],"status":"cancelled"}
        after=source.stat()
        if (before.st_size,before.st_mtime_ns)!=(after.st_size,after.st_mtime_ns) or _digest(source)!=media_id: raise SearchError("source changed while indexing")
        db.execute("UPDATE media SET status='complete' WHERE media_id=? AND pipeline=?",(media_id,pipeline))
        db.execute("UPDATE media_pipeline_state SET status='complete' WHERE media_id=? AND pipeline=?",(media_id,pipeline)); db.commit()
        report(stage="complete",done=len(starts),total=len(starts),video_name=source.name,device=model_device)
      except Exception:
        try:
            db.execute("UPDATE media SET status='failed' WHERE media_id=? AND pipeline=?",(media_id,pipeline))
            db.execute("UPDATE media_pipeline_state SET status='failed' WHERE media_id=? AND pipeline=?",(media_id,pipeline))
            db.commit()
        except sqlite3.Error:
            db.rollback()
        raise
      finally: cap.release(); db.close()
    return {"media_id":media_id,"windows_added":done,"duration_s":round(duration,3),"pipeline":pipeline,"model_version":model_version,"preprocess_version":preprocess_version,"window_version":window_version,"device":model_device,"decode_seconds":decode_s,"feature_seconds":feature_s,"persist_seconds":persist_s,"terminal_seek_retry_windows":terminal_seek_retries[0],"status":"complete"}

def _query_feature(video: Path, start: float, end: float, *, engine: str="color-motion", checkpoint_path: str|None=None):
    if not math.isfinite(start) or not math.isfinite(end) or start<0 or end<=start:
        raise SearchError("query interval must have finite, non-negative start and end after start")
    if abs((end-start)-WINDOW_S)>QUERY_DURATION_TOLERANCE_S:
        raise SearchError(f"query interval must match indexed window length {WINDOW_S:g}s within {QUERY_DURATION_TOLERANCE_S:g}s")
    source=resolve_local_media(video)
    cap=_open_local_video(source)
    if not cap.isOpened(): raise SearchError("cannot decode query video")
    fps=float(cap.get(cv2.CAP_PROP_FPS) or 0)
    frames=_sample(cap,fps,start,end,count=64 if engine=="vjepa2" else 8); cap.release()
    if not frames: raise SearchError("query interval is outside video or cannot be decoded")
    if engine=="vjepa2":
      from .model import load_raw_vjepa2,extract_features,PROCESSOR_CONFIG_VERSION
      model,processor,device,model_version=load_raw_vjepa2(checkpoint_path)
      feature=extract_features([cv2.cvtColor(f,cv2.COLOR_BGR2RGB) for f in frames],model,processor)
      return feature,device,model_version,PROCESSOR_CONFIG_VERSION,"2s-stride-0.5s-fpc64-terminal-2frame-retry-v2"
    return _feature(frames),"OpenCV CPU",MODEL_VERSION,PREPROCESS_VERSION,WINDOW_VERSION

def search(video: Path, start: float, end: float, store: Path, *, limit: int=20, suppress_s: float=1.0, engine: str="color-motion", checkpoint_path: str|None=None) -> list[dict]:
    if engine not in {"color-motion","vjepa2"}: raise SearchError("engine must be color-motion or vjepa2")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_SEARCH_RESULTS:
        raise SearchError(f"limit must be between 1 and {MAX_SEARCH_RESULTS}")
    if not math.isfinite(suppress_s) or suppress_s < 0:
        raise SearchError("suppress_s must be a finite, non-negative value")
    source=resolve_local_media(video)
    # Reject a store that aliases the query source before decoding or loading a
    # model, and pass the same canonical source through to the final DB open.
    _db_path(store,source)
    q,device,model_version,preprocess_version,window_version=_query_feature(source,start,end,engine=engine,checkpoint_path=checkpoint_path)
    pipeline=_pipeline_id(engine,model_version,preprocess_version,window_version,device)
    db=connect_readonly(store,source)
    schema_version=int(db.execute("PRAGMA user_version").fetchone()[0])
    has_pipeline_state=db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='media_pipeline_state'").fetchone() is not None
    if schema_version>=2 and not has_pipeline_state:
        db.close()
        raise SearchError("index database schema is incomplete")
    if has_pipeline_state:
        rows=db.execute("""SELECT w.media_id,w.start_s,w.end_s,w.feature,m.path
            FROM windows AS w JOIN media AS m USING(media_id)
            JOIN media_pipeline_state AS p ON p.media_id=w.media_id AND p.pipeline=w.pipeline
            WHERE w.pipeline=? AND w.model_version=? AND w.preprocess_version=?
            AND w.window_version=? AND p.status='complete'
            ORDER BY w.media_id,w.start_s LIMIT ?""",(pipeline,model_version,preprocess_version,window_version,MAX_SEARCH_INDEX_WINDOWS+1)).fetchall()
    else:
        # Legacy schema has no per-pipeline state table. Its only searchable
        # pipeline is the one represented by media.pipeline/status.
        rows=db.execute("""SELECT w.media_id,w.start_s,w.end_s,w.feature,m.path
            FROM windows AS w JOIN media AS m USING(media_id)
            WHERE w.pipeline=? AND w.model_version=? AND w.preprocess_version=?
            AND w.window_version=? AND m.pipeline=w.pipeline AND m.status='complete'
            ORDER BY w.media_id,w.start_s LIMIT ?""",(pipeline,model_version,preprocess_version,window_version,MAX_SEARCH_INDEX_WINDOWS+1)).fetchall()
    db.close()
    if len(rows) > MAX_SEARCH_INDEX_WINDOWS:
        raise SearchError("index exceeds the in-memory search window limit; narrow or split the store")
    scored=[]
    for mid,s,e,blob,path in rows:
      f=np.frombuffer(blob,dtype=np.float32); score=float(np.dot(q,f)/(np.linalg.norm(q)*np.linalg.norm(f)+1e-12))
      scored.append({"media_id":mid,"path":path,"start_s":s,"end_s":e,"score":score,"engine":engine,"device":device,"pipeline":pipeline})
    scored.sort(key=lambda r:r['score'],reverse=True)
    kept=[]
    for item in scored:
      if any(k['media_id']==item['media_id'] and abs(k['start_s']-item['start_s'])<suppress_s for k in kept): continue
      kept.append(item)
      if len(kept)>=limit: break
    return kept

def decide(store: Path, media_id: str, start: float, decision: str, *, engine: str="color-motion", pipeline: str|None=None, checkpoint_path: str|None=None) -> None:
    if decision not in {'keep','hold'}: raise SearchError("decision must be keep or hold")
    if engine not in {"color-motion","vjepa2"}: raise SearchError("engine must be color-motion or vjepa2")
    db=connect(store)
    if pipeline is None:
        if engine=="color-motion":
            pipeline=_pipeline_id(engine,MODEL_VERSION,PREPROCESS_VERSION,WINDOW_VERSION,"opencv-cpu")
        else:
            candidates=db.execute("SELECT DISTINCT pipeline FROM windows WHERE media_id=? AND start_s=? AND pipeline LIKE 'vjepa2-%'",(media_id,float(start))).fetchall()
            if len(candidates)!=1:
                db.close(); raise SearchError("V-JEPA decision needs an unambiguous indexed candidate pipeline")
            pipeline=candidates[0][0]
    exists=db.execute("""SELECT 1 FROM windows AS w
        JOIN media_pipeline_state AS p ON p.media_id=w.media_id AND p.pipeline=w.pipeline
        WHERE w.media_id=? AND w.pipeline=? AND w.start_s=? AND p.status='complete'""",
        (media_id,pipeline,float(start))).fetchone()
    if not exists:
        db.close(); raise SearchError("decision must reference an indexed window with the same feature fingerprint")
    db.execute("INSERT OR REPLACE INTO decisions VALUES(?,?,?,?)",(media_id,pipeline,float(start),decision)); db.commit(); db.close()

def references(store: Path) -> list[dict]:
    db=connect(store); rows=db.execute("SELECT d.media_id,m.path,d.pipeline,d.start_s,w.end_s,d.decision FROM decisions d JOIN media m USING(media_id) LEFT JOIN windows w ON w.media_id=d.media_id AND w.pipeline=d.pipeline AND w.start_s=d.start_s ORDER BY d.media_id,d.pipeline,d.start_s").fetchall(); db.close()
    return [dict(media_id=a,path=b,pipeline=c,start_s=d,end_s=e,decision=f) for a,b,c,d,e,f in rows]

def runtime_info() -> dict:
    import platform, sys
    info={"platform":platform.platform(),"machine":platform.machine(),"python":sys.version.split()[0],"opencv":cv2.__version__,"numpy":np.__version__,"backend":"OpenCV CPU baseline"}
    try:
      import torch
      info.update(torch=torch.__version__,mps_built=torch.backends.mps.is_built(),mps_available=torch.backends.mps.is_available(),device='mps' if torch.backends.mps.is_available() else 'cpu')
    except ImportError: info.update(torch="not installed",mps_available=False,device="cpu (JEPA unavailable)")
    return info
