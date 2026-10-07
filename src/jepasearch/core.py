from __future__ import annotations
import fcntl, hashlib, json, math, os, re, shutil, sqlite3, threading, time
from pathlib import Path
from typing import Callable
import cv2
import numpy as np

PIPELINE = "color-motion-v1"
MODEL_VERSION = "none-baseline-v1"
PREPROCESS_VERSION = "rgb-grid-4x4-motion-v1"
WINDOW_VERSION = "2s-stride-0.5s-samples-8-v1"
WINDOW_S, STRIDE_S = 2.0, 0.5
QUERY_DURATION_TOLERANCE_S = 1e-6

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
    store.parent.mkdir(parents=True, exist_ok=True)
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

def connect(store: Path, source: Path | None = None) -> sqlite3.Connection:
    path = _db_path(store, source)
    db = sqlite3.connect(path, timeout=30)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=FULL")
    db.executescript("""
      CREATE TABLE IF NOT EXISTS media(media_id TEXT PRIMARY KEY, path TEXT NOT NULL, size INTEGER NOT NULL, mtime_ns INTEGER NOT NULL, duration_s REAL NOT NULL, fps REAL NOT NULL, width INTEGER NOT NULL, height INTEGER NOT NULL, pipeline TEXT NOT NULL, status TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS windows(media_id TEXT NOT NULL, pipeline TEXT NOT NULL, model_version TEXT NOT NULL, preprocess_version TEXT NOT NULL, window_version TEXT NOT NULL, start_s REAL NOT NULL, end_s REAL NOT NULL, feature BLOB NOT NULL, PRIMARY KEY(media_id,pipeline,model_version,preprocess_version,window_version,start_s));
      CREATE TABLE IF NOT EXISTS decisions(media_id TEXT NOT NULL, pipeline TEXT NOT NULL, start_s REAL NOT NULL, decision TEXT NOT NULL, PRIMARY KEY(media_id,pipeline,start_s));
    """)
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

def _sample(cap, fps: float, start: float, end: float, count: int=8) -> list[np.ndarray]:
    frames=[]
    for t in np.linspace(start, max(start,end-1/max(fps,1)), count):
        cap.set(cv2.CAP_PROP_POS_MSEC, float(t)*1000)
        ok,frame=cap.read()
        if not ok: return []
        frames.append(frame)
    return frames

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
        preprocess_version=PROCESSOR_CONFIG_VERSION; window_version="2s-stride-0.5s-fpc64-v1"
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
    starts=np.arange(0,max(0,duration-WINDOW_S)+1e-8,STRIDE_S)
    estimate=int(len(starts)*8192+65536)
    if shutil.disk_usage(store_path.parent).free < estimate+reserve_bytes:
        cap.release(); raise SearchError("insufficient free space; indexing stopped before creating the store")
    db=connect(store,source)
    lock_path=Path(str(store_path)+".lock")
    with lock_path.open('a') as lock:
      try: fcntl.flock(lock, fcntl.LOCK_EX|fcntl.LOCK_NB)
      except BlockingIOError as e: cap.release(); db.close(); raise SearchError("another indexing run is active") from e
      existing=db.execute("SELECT pipeline,status FROM media WHERE media_id=?",(media_id,)).fetchone()
      if existing and existing[0]==pipeline and existing[1]=='complete' and not resume:
          cap.release(); db.close(); raise SearchError("already indexed; use resume or a new store")
      db.execute("INSERT OR REPLACE INTO media VALUES(?,?,?,?,?,?,?,?,?,?)",(media_id,str(source),before.st_size,before.st_mtime_ns,duration,fps,w,h,pipeline,'indexing')); db.commit()
      done=0; decode_s=0.0; feature_s=0.0; persist_s=0.0
      processed=0
      report(stage="indexing",done=0,total=len(starts),video_name=source.name,device=model_device)
      cancelled=False
      try:
        for start in starts:
          if cancel_event is not None and cancel_event.is_set():
              db.execute("UPDATE media SET status='interrupted' WHERE media_id=?",(media_id,)); db.commit()
              cancelled=True
              report(stage="interrupted",done=processed,total=len(starts),video_name=source.name,device=model_device)
              break
          exists=db.execute("SELECT 1 FROM windows WHERE media_id=? AND pipeline=? AND model_version=? AND preprocess_version=? AND window_version=? AND start_s=?",(media_id,pipeline,model_version,preprocess_version,window_version,float(start))).fetchone()
          if exists:
              processed+=1
              report(stage="indexing",done=processed,total=len(starts),video_name=source.name,device=model_device)
              continue
          phase_t=time.perf_counter(); frames=_sample(cap,fps,float(start),float(start)+WINDOW_S,count=64 if engine=="vjepa2" else 8); decode_s+=time.perf_counter()-phase_t
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
            return {"media_id":media_id,"windows_added":done,"duration_s":round(duration,3),"pipeline":pipeline,"model_version":model_version,"preprocess_version":preprocess_version,"window_version":window_version,"device":model_device,"decode_seconds":decode_s,"feature_seconds":feature_s,"persist_seconds":persist_s,"status":"cancelled"}
        after=source.stat()
        if (before.st_size,before.st_mtime_ns)!=(after.st_size,after.st_mtime_ns) or _digest(source)!=media_id: raise SearchError("source changed while indexing")
        db.execute("UPDATE media SET status='complete' WHERE media_id=?",(media_id,)); db.commit()
        report(stage="complete",done=len(starts),total=len(starts),video_name=source.name,device=model_device)
      finally: cap.release(); db.close()
    return {"media_id":media_id,"windows_added":done,"duration_s":round(duration,3),"pipeline":pipeline,"model_version":model_version,"preprocess_version":preprocess_version,"window_version":window_version,"device":model_device,"decode_seconds":decode_s,"feature_seconds":feature_s,"persist_seconds":persist_s,"status":"complete"}

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
      return feature,device,model_version,PROCESSOR_CONFIG_VERSION,"2s-stride-0.5s-fpc64-v1"
    return _feature(frames),"OpenCV CPU",MODEL_VERSION,PREPROCESS_VERSION,WINDOW_VERSION

def search(video: Path, start: float, end: float, store: Path, *, limit: int=20, suppress_s: float=1.0, engine: str="color-motion", checkpoint_path: str|None=None) -> list[dict]:
    if engine not in {"color-motion","vjepa2"}: raise SearchError("engine must be color-motion or vjepa2")
    q,device,model_version,preprocess_version,window_version=_query_feature(video,start,end,engine=engine,checkpoint_path=checkpoint_path)
    pipeline=_pipeline_id(engine,model_version,preprocess_version,window_version,device)
    db=connect(store)
    rows=db.execute("SELECT w.media_id,w.start_s,w.end_s,w.feature,m.path FROM windows w JOIN media m USING(media_id) WHERE w.pipeline=? AND m.pipeline=w.pipeline AND w.model_version=? AND w.preprocess_version=? AND w.window_version=? AND m.status='complete' ORDER BY w.media_id,w.start_s",(pipeline,model_version,preprocess_version,window_version)).fetchall(); db.close()
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
    exists=db.execute("SELECT 1 FROM windows w JOIN media m USING(media_id) WHERE w.media_id=? AND w.pipeline=? AND w.start_s=? AND m.pipeline=w.pipeline AND m.status='complete'",(media_id,pipeline,float(start))).fetchone()
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
