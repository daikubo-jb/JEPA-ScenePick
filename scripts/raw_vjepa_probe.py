from __future__ import annotations
import gc, json, resource, sys, time
from pathlib import Path

import numpy as np
import torch
from transformers import VJEPA2VideoProcessor

PROJECT = Path(__file__).resolve().parents[1]
PDEV = PROJECT.parent
RISK_ROOT = PDEV / "jepa-risk-detection"
CHECKPOINT = RISK_ROOT / "artifacts" / "checkpoints" / "vitl.pt"


def synthetic_frames() -> np.ndarray:
    frames = np.zeros((64, 256, 256, 3), dtype=np.uint8)
    palette = np.asarray([[210, 70, 32], [30, 185, 50], [35, 75, 215]], dtype=np.uint8)
    for i, frame in enumerate(frames):
        if i >= 22:
            frame[:] = 16
        if i >= 43:
            frame[:] = 32
        x = (i * 3) % 216
        frame[96:160, x:x + 40] = palette[min(2, i // 22)]
        frame[28:48, 180 - x // 2:200 - x // 2] = palette[(i // 16 + 1) % 3]
    return frames


def main() -> int:
    if not CHECKPOINT.is_file():
        raise RuntimeError("the configured local raw checkpoint is unavailable")
    sys.path.insert(0, str(RISK_ROOT / "src"))
    from jepa_risk.predictor import load_official_bundle

    device = torch.device("mps" if torch.backends.mps.is_available() else "cpu")
    config = json.loads((PROJECT / "configs" / "vjepa2-vitl-fpc64-256-video-preprocessor.json").read_text())
    processor_args = {k: v for k, v in config.items() if k not in {"_valid_kwargs_names", "model_valid_processing_keys", "video_processor_type"}}
    processor = VJEPA2VideoProcessor(**processor_args)
    frames = synthetic_frames()
    start = time.perf_counter()
    bundle = load_official_bundle(CHECKPOINT, device=device, dtype=torch.float32)
    load_seconds = time.perf_counter() - start
    encoder = bundle.encoder
    bundle.target_encoder = None
    bundle.predictor = None
    del bundle
    gc.collect()

    start = time.perf_counter()
    encoded = processor([frames], do_resize=False, do_center_crop=False, input_data_format="channels_last", return_tensors="pt")
    pixel_values = encoded["pixel_values_videos"].permute(0, 2, 1, 3, 4).to(device=device, dtype=torch.float32)
    preprocess_seconds = time.perf_counter() - start
    start = time.perf_counter()
    with torch.inference_mode():
        tokens = encoder(pixel_values)
        if tokens.shape != (1, 8192, 1024):
            raise RuntimeError(f"unexpected raw encoder output shape: {tuple(tokens.shape)}")
        pooled = tokens.reshape(1, 32, 16, 16, 1024).float().mean(dim=(1, 2, 3))
        if not torch.isfinite(pooled).all():
            raise RuntimeError("raw encoder returned non-finite features")
    inference_seconds = time.perf_counter() - start
    peak_rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    if sys.platform != "darwin":
        peak_rss *= 1024
    print(json.dumps({
        "synthetic_frames": 64,
        "checkpoint_bytes": CHECKPOINT.stat().st_size,
        "loader": "jepa-risk-detection.load_official_bundle; torch.load(weights_only=True); strict state-dict load",
        "device": str(device),
        "mps_built": torch.backends.mps.is_built(),
        "mps_available": torch.backends.mps.is_available(),
        "processor_shape": list(encoded["pixel_values_videos"].shape),
        "token_shape": list(tokens.shape),
        "pooled_shape": list(pooled.shape),
        "finite": bool(torch.isfinite(pooled).all()),
        "load_seconds": round(load_seconds, 3),
        "preprocess_seconds": round(preprocess_seconds, 3),
        "inference_seconds": round(inference_seconds, 3),
        "peak_rss_bytes": int(peak_rss),
        "real_media_accessed": False,
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
