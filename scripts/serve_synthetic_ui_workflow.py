"""Start an isolated UI workflow using only the checked-in synthetic H.264 clip."""
from __future__ import annotations

import argparse
import tempfile
from pathlib import Path

from jepasearch.ui import LocalUIService, make_server


class SyntheticWorkflowService(LocalUIService):
    def __init__(self, video: Path, store: Path):
        self.synthetic_video = video
        self.synthetic_store = store
        store.mkdir(parents=True, exist_ok=True)
        super().__init__(picker=self._pick)

    def _pick(self, kind: str) -> list[Path]:
        if kind == "file":
            return [self.synthetic_video]
        if kind == "folder":
            return [self.synthetic_store]
        return []

    def default_store_dir(self) -> Path:
        return self.synthetic_store


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Synthetic-only UI workflow server")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--engine", choices=["color-motion", "vjepa2"], required=True)
    args = parser.parse_args(argv)

    video = Path(__file__).resolve().parents[1] / "samples" / "synthetic-court-motion-h264.mp4"
    if not video.is_file():
        raise SystemExit(f"Persistent synthetic H.264 fixture is missing: {video}")
    with tempfile.TemporaryDirectory(prefix="jepa-ui-workflow-") as directory:
        root = Path(directory)
        service = SyntheticWorkflowService(video, root / "store")
        server = make_server(args.port, service=service)
        print(f"Synthetic H.264 UI workflow ready: engine={args.engine} port={server.server_port}; picker returns only {video.name}", flush=True)
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
