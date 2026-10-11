from __future__ import annotations
import gc
import hashlib
import hmac
import json
import os
import re
import subprocess
import sys
from pathlib import Path

from .core import SearchError

META_SOURCE_COMMIT = "204698b45b3712590f06245fbfba32d3be539812"
HF_MODEL_REVISION = "b3c1679b7c34d3255ef3547f27c7b226aefab26f"
PROCESSOR_CONFIG_VERSION = "vjepa2-vitl-fpc64-256-processor-b3c1679b7c34"
PINNED_LOADER_COMMIT = "4b2c30ab02fd4d951084a5439359c158d31f0c66"
PINNED_LOADER_FILES = {
    "src/jepa_risk/predictor.py": "f0b195d1e56a9d4391d538be1ba24f62508074cbbfa7dd5182f29e429cff7155",
    "src/jepa_risk/features.py": "3a3ea8433e8730e2cbe4a66fb22f0690094bfae1496f5403a0b6e1ad424b3c67",
}
PINNED_MODEL_PACKAGES = {
    "numpy": "2.2.6",
    "Pillow": "11.2.1",
    "torch": "2.7.1",
    "torchvision": "0.22.1",
    "transformers": "4.53.3",
}


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


def _identity_path() -> Path:
    project_root = Path(__file__).resolve().parents[2]
    return Path(os.environ.get("JEPA_MODEL_IDENTITY_PATH", project_root / ".local" / "checkpoint-identity.json")).expanduser()


def _load_local_identity(checkpoint: Path) -> dict | None:
    """Read the explicit local pin record; this does not read checkpoint bytes."""
    path = _identity_path()
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as exc:
        raise SearchError("local V-JEPA checkpoint identity record is unreadable") from exc
    if not isinstance(record, dict) or record.get("schema_version") != 1:
        raise SearchError("local V-JEPA checkpoint identity record has an unsupported schema")
    expected = str(record.get("sha256", "")).casefold()
    if not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise SearchError("local V-JEPA checkpoint identity record has no valid SHA-256 pin")
    try:
        registered_path = Path(record["checkpoint_path"]).expanduser().resolve(strict=True)
        configured_path = checkpoint.expanduser().resolve(strict=True)
    except (KeyError, OSError) as exc:
        raise SearchError("local V-JEPA checkpoint identity path is unavailable") from exc
    if registered_path != configured_path:
        raise SearchError("local V-JEPA checkpoint does not match its registered path")
    if not isinstance(record.get("source"), dict) or not record["source"].get("description"):
        raise SearchError("local V-JEPA checkpoint identity has no source description")
    configured = os.environ.get("JEPA_RAW_CHECKPOINT_SHA256", "").strip().casefold()
    if configured and not hmac.compare_digest(expected, configured):
        raise SearchError("JEPA_RAW_CHECKPOINT_SHA256 differs from the registered local pin")
    return record


def raw_model_version(checkpoint_path: str | None = None) -> str:
    _risk_root, default_checkpoint = _default_paths()
    checkpoint = Path(checkpoint_path).expanduser() if checkpoint_path else default_checkpoint
    if not checkpoint.is_file():
        raise SearchError("configured local V-JEPA2 raw checkpoint is unavailable")
    identity = _load_local_identity(checkpoint)
    if identity is None:
        raise SearchError("register the existing local checkpoint in .local/checkpoint-identity.json before using V-JEPA2")
    expected = str(identity["sha256"]).casefold()
    digest = _sha256(checkpoint)
    if not hmac.compare_digest(digest, expected):
        raise SearchError("configured local V-JEPA2 checkpoint does not match its SHA-256 pin")
    return f"meta-vjepa2-vitl-{META_SOURCE_COMMIT[:12]}-loader-{PINNED_LOADER_COMMIT[:12]}-{digest}"


def _verify_pinned_loader(risk_root: Path) -> None:
    try:
        root = risk_root.resolve(strict=True)
        result = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise SearchError("the pinned local V-JEPA2 loader checkout is unavailable") from exc
    if result.stdout.strip() != PINNED_LOADER_COMMIT:
        raise SearchError("the local V-JEPA2 loader checkout does not match its pinned commit")
    try:
        status = subprocess.run(
            ["git", "-C", str(root), "status", "--porcelain", "--untracked-files=all", "--", "src/jepa_risk"],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise SearchError("the local V-JEPA2 loader source status could not be verified") from exc
    if status.stdout.strip():
        raise SearchError("the local V-JEPA2 loader source tree has unpinned changes")
    for relative, expected in PINNED_LOADER_FILES.items():
        source = root / relative
        try:
            if source.is_symlink() or not source.is_file() or _sha256(source) != expected:
                raise SearchError("the local V-JEPA2 loader source does not match its pinned digest")
        except OSError as exc:
            raise SearchError("the pinned local V-JEPA2 loader source is unavailable") from exc


def _verify_model_packages() -> None:
    from importlib.metadata import PackageNotFoundError, version
    for package, expected in PINNED_MODEL_PACKAGES.items():
        try:
            actual = version(package)
        except PackageNotFoundError as exc:
            raise SearchError("the pinned local NumPy/PyTorch/TorchVision/Transformers dependencies are required") from exc
        if actual != expected:
            raise SearchError("installed local V-JEPA2 dependencies do not match their pinned versions")


def _installed_model_versions() -> dict[str, str]:
    from importlib.metadata import PackageNotFoundError, version
    installed = {}
    for package in PINNED_MODEL_PACKAGES:
        try:
            installed[package] = version(package)
        except PackageNotFoundError:
            installed[package] = "not-installed"
    return installed


def vjepa2_readiness() -> dict:
    """Report local V-JEPA configuration without opening checkpoint bytes."""
    risk_root, checkpoint = _default_paths()
    blockers = []
    if not checkpoint.is_file():
        blockers.append("ローカルV-JEPA 2 checkpointが見つかりません。JEPA_RAW_CHECKPOINTを設定してください。")

    identity = None
    identity_error = None
    if checkpoint.is_file():
        try:
            identity = _load_local_identity(checkpoint)
        except SearchError as exc:
            identity_error = str(exc)
    if identity is None:
        blockers.append(identity_error or "既存checkpointの明示的なローカルSHA-256登録がありません。scripts/register_local_checkpoint.pyを一度実行してください。")

    try:
        _verify_pinned_loader(risk_root)
    except SearchError:
        blockers.append("V-JEPA loader checkoutが固定commit/source digestと一致しません。")

    installed = _installed_model_versions()
    for package, required in PINNED_MODEL_PACKAGES.items():
        actual = installed.get(package, "not-installed")
        if actual != required:
            blockers.append(f"{package} {required}が必要です（このPython: {actual}）。")

    if blockers:
        message = "V-JEPA 2は現在利用できません。" + " ".join(blockers)
    else:
        message = (
            "ローカルcheckpointの初回登録と依存pinが揃っています。SHA-256はこの既存ファイルの後続整合性を確認するローカルpinです。"
            "提供元の真正性は署名/公式checksumで検証できておらず、この記録だけでは証明しません。"
        )
    return {
        "available": not blockers,
        "message": message,
        "blockers": blockers,
        "checkpoint_hash_configured": identity is not None,
        "hash_source_verified": False,
        "local_pin_registered": identity is not None,
        "provider_authentication": "unverified" if identity is not None else "not-registered",
        "dependencies": installed,
    }


def load_raw_vjepa2(checkpoint_path: str | None = None):
    """Load the existing Meta-format ViT-L checkpoint through its pinned local loader."""
    risk_root, default_checkpoint = _default_paths()
    checkpoint = Path(checkpoint_path).expanduser() if checkpoint_path else default_checkpoint
    if not checkpoint.is_file():
        raise SearchError("configured local V-JEPA2 raw checkpoint is unavailable")
    checkpoint_version = raw_model_version(str(checkpoint))
    source = risk_root / "src"
    if not (source / "jepa_risk" / "predictor.py").is_file():
        raise SearchError("the pinned local V-JEPA2 loader source is unavailable")
    _verify_pinned_loader(risk_root)

    try:
        import torch
        from transformers import VJEPA2VideoProcessor
        sys.path.insert(0, str(source))
        from jepa_risk.features import resolve_device
        from jepa_risk.predictor import load_official_bundle
        _verify_model_packages()
    except Exception as exc:
        raise SearchError("the pinned local runtime dependencies and raw-checkpoint loader are required") from exc

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
