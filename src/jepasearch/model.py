from __future__ import annotations
import gc
import hashlib
import json
import os
import sys
from pathlib import Path

from .core import SearchError

META_SOURCE_COMMIT = "204698b45b3712590f06245fbfba32d3be539812"
HF_MODEL_REVISION = "b3c1679b7c34d3255ef3547f27c7b226aefab26f"
PROCESSOR_CONFIG_VERSION = "vjepa2-vitl-fpc64-256-processor-b3c1679b7c34"


def _default_paths() -> tuple[Path, Path]:
    project_root = Path(__file__).resolve().parents[2]
    risk_root = Path(os.environ.get("JEPA_RISK_ROOT", project_root.parent / "jepa-risk-detection")).expanduser()
    checkpoint = Path(os.environ.get("JEPA_RAW_CHECKPOINT", risk_root / "artifacts" / "checkpoints" / "vitl.pt")).expanduser()
    return risk_root, checkpoint


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def raw_model_version(checkpoint_path: str | None = None) -> str:
    _risk_root, default_checkpoint = _default_paths()
    checkpoint = Path(checkpoint_path).expanduser() if checkpoint_path else default_checkpoint
    if not checkpoint.is_file():
        raise SearchError("configured local V-JEPA2 raw checkpoint is unavailable")
    return f"meta-vjepa2-vitl-{META_SOURCE_COMMIT[:12]}-{_sha256(checkpoint)[:16]}"


def load_raw_vjepa2(checkpoint_path: str | None = None):
    """Load the existing Meta-format ViT-L checkpoint through its pinned local loader."""
    risk_root, default_checkpoint = _default_paths()
    checkpoint = Path(checkpoint_path).expanduser() if checkpoint_path else default_checkpoint
    if not checkpoint.is_file():
        raise SearchError("configured local V-JEPA2 raw checkpoint is unavailable")
    source = risk_root / "src"
    if not (source / "jepa_risk" / "predictor.py").is_file():
        raise SearchError("the pinned local V-JEPA2 loader source is unavailable")

    try:
        import torch
        from transformers import VJEPA2VideoProcessor
        sys.path.insert(0, str(source))
        from jepa_risk.features import resolve_device
        from jepa_risk.predictor import load_official_bundle
    except Exception as exc:
        raise SearchError("the local PyTorch/Transformers and pinned raw-checkpoint loader are required") from exc

    processor_config_path = Path(__file__).resolve().parents[2] / "configs" / "vjepa2-vitl-fpc64-256-video-preprocessor.json"
    try:
        cfg = json.loads(processor_config_path.read_text(encoding="utf-8"))
        args = {k: v for k, v in cfg.items() if k not in {"_valid_kwargs_names", "model_valid_processing_keys", "video_processor_type"}}
        processor = VJEPA2VideoProcessor(**args)
        device = resolve_device("auto")
        bundle = load_official_bundle(checkpoint, device=device, dtype=torch.float32)
        encoder = bundle.encoder
        del bundle.target_encoder
        del bundle.predictor
        del bundle
        gc.collect()
        checkpoint_version = raw_model_version(str(checkpoint))
    except Exception as exc:
        raise SearchError("the existing raw checkpoint or pinned processor config is incompatible") from exc
    return encoder, processor, str(device), checkpoint_version


def extract_features(frames_rgb, encoder, processor):
    """Pool full 64-frame ViT-L vision tokens from the Meta raw checkpoint encoder."""
    try:
        import numpy as np
        import torch
    except ImportError as exc:
        raise SearchError("PyTorch and NumPy are required for V-JEPA2 feature extraction") from exc
    if len(frames_rgb) != 64:
        raise SearchError("V-JEPA2 fpc64 requires exactly 64 input frames")
    frames = np.stack([np.asarray(frame, dtype=np.uint8) for frame in frames_rgb])
    # Keep the frozen official processor's resize (shortest edge 292) and center crop (256x256).
    encoded = processor([frames], input_data_format="channels_last", return_tensors="pt")
    pixel_values = encoded["pixel_values_videos"]
    if tuple(pixel_values.shape) != (1, 64, 3, 256, 256):
        raise SearchError(f"unexpected V-JEPA2 processor shape {tuple(pixel_values.shape)}")
    model_device = next(encoder.parameters()).device
    model_dtype = next(encoder.parameters()).dtype
    videos = pixel_values.permute(0, 2, 1, 3, 4).to(device=model_device, dtype=model_dtype)
    with torch.inference_mode():
        tokens = encoder(videos)
    if tuple(tokens.shape) != (1, 8192, 1024):
        raise SearchError(f"unexpected raw encoder token shape {tuple(tokens.shape)}")
    pooled = tokens.reshape(1, 32, 16, 16, 1024).float().mean(dim=(1, 2, 3))
    if not torch.isfinite(pooled).all():
        raise SearchError("V-JEPA2 returned non-finite features")
    return pooled[0].cpu().numpy()
