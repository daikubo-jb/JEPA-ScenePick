"""Serve a synthetic-only pre-indexed UI fixture for manual visual QA."""
from __future__ import annotations

import argparse
import secrets
import tempfile
import threading
from pathlib import Path

from jepasearch.core import index_video, resolve_local_media, search
from jepasearch.ui import LocalMedia, LocalUIService, make_server


class SyntheticDemoService(LocalUIService):
    def __init__(self, fixture: Path, store_dir: Path):
        super().__init__(picker=lambda kind: [fixture if kind == "file" else store_dir])
        self.fixture = fixture
        self.demo_store = store_dir

    def new_session(self):
        session = super().new_session()
        session.store_dir = self.demo_store
        path = resolve_local_media(self.fixture)
        stat = path.stat()
        file_id = secrets.token_hex(16)
        indexed = index_video(path, self.demo_store / "index.sqlite", reserve_bytes=0, engine="color-motion")
        entry = LocalMedia(file_id, path, path.name, stat.st_size, stat.st_mtime_ns, stat.st_dev, stat.st_ino,
                           indexed["media_id"], {str(self.demo_store): {"color-motion": indexed["pipeline"]}})
        session.files[file_id] = entry
        session.indexed_media[indexed["media_id"]] = file_id
        matches = search(path, 0.0, 2.0, self.demo_store / "index.sqlite", engine="color-motion")
        results = []
        for match in matches:
            candidate_id = secrets.token_hex(16)
            candidate = {"id": candidate_id, "name": path.name, "file_id": file_id,
                         "media_id": match["media_id"], "start_s": match["start_s"], "end_s": match["end_s"],
                         "score": match["score"], "pipeline": match["pipeline"], "engine": match["engine"],
                         "fingerprint": match["pipeline"], "device": match["device"],
                         "preview_available": True, "decision": None}
            session.candidates[candidate_id] = candidate
            results.append({key: value for key, value in candidate.items() if key not in {"media_id", "pipeline"}})
        fingerprint = results[0]["fingerprint"] if results else indexed["pipeline"]
        self.job = {"id": secrets.token_hex(16), "owner": session.token, "kind": "search", "status": "complete",
                    "stage": "complete", "done": len(matches), "total": len(matches), "current_name": path.name,
                    "device": "OpenCV CPU", "message": "合成デモの候補を表示しています", "error": None,
                    "engine": "color-motion", "fingerprint": fingerprint, "results": results}
        self.cancel_event = threading.Event()
        return session


def main(argv=None):
    parser = argparse.ArgumentParser(description="Synthetic-only local UI visual QA server")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args(argv)
    fixture = Path(__file__).resolve().parents[1] / "samples" / "synthetic-court-motion-h264.mp4"
    if not fixture.is_file():
        raise SystemExit(f"Persistent synthetic fixture is missing: {fixture}")
    with tempfile.TemporaryDirectory(prefix="jepa-ui-synthetic-demo-") as temporary:
        root = Path(temporary)
        service = SyntheticDemoService(fixture, root / "local-index")
        server = make_server(args.port, service=service)
        url = f"http://127.0.0.1:{server.server_port}/"
        print(f"Synthetic UI demo: {url}", flush=True)
        print(f"Synthetic fixture: {fixture.name}; no user media used", flush=True)
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
