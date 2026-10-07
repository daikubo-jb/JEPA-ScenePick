from __future__ import annotations
import json, resource, sys, tempfile, time
from pathlib import Path

import cv2
import numpy as np
from jepasearch.cli import main as cli_main
from jepasearch.core import decide, index_video, references, search
from jepasearch.synthetic import make_video


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="jepa-vjepa-synth-") as tmp:
        root = Path(tmp)
        video = make_video(root / "synthetic.mp4", duration=2.0, fps=32)
        store = root / "index.sqlite"
        started = time.perf_counter()
        indexed = index_video(video, store, reserve_bytes=0, engine="vjepa2")
        index_total = time.perf_counter() - started
        started = time.perf_counter()
        matches = search(video, 0.0, 2.0, store, limit=5, engine="vjepa2")
        search_total = time.perf_counter() - started
        if not matches:
            raise RuntimeError("synthetic query returned no candidates")
        match = matches[0]
        decide(store, match["media_id"], match["start_s"], "keep", engine="vjepa2", pipeline=match["pipeline"])
        refs = references(store)
        if len(refs) != 1 or refs[0]["decision"] != "keep":
            raise RuntimeError("synthetic keep decision was not persisted")
        peak_rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        if sys.platform != "darwin":
            peak_rss *= 1024
        db_bytes = sum(p.stat().st_size for p in root.glob("index.sqlite*") if p.is_file())
        result = {
            "synthetic_only": True,
            "real_media_accessed": False,
            "video_bytes": video.stat().st_size,
            "index_bytes": db_bytes,
            "windows": indexed["windows_added"],
            "decode_seconds": round(indexed["decode_seconds"], 3),
            "preprocess_and_encoder_seconds": round(indexed["feature_seconds"], 3),
            "persistence_seconds": round(indexed["persist_seconds"], 4),
            "index_total_seconds_including_raw_loader": round(index_total, 3),
            "search_inference_and_query_seconds_including_raw_loader": round(search_total, 3),
            "search_candidates": len(matches),
            "first_score": round(match["score"], 6),
            "first_match_start_s": match["start_s"],
            "first_match_end_s": match["end_s"],
            "device": indexed["device"],
            "pipeline": indexed["pipeline"],
            "model_version": indexed["model_version"],
            "preprocess_version": indexed["preprocess_version"],
            "window_version": indexed["window_version"],
            "feature_dimensions": 1024,
            "peak_rss_bytes": int(peak_rss),
            "quality_claim": "none; synthetic functional smoke only",
        }
        print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
