#!/usr/bin/env python3
"""Register the pre-existing approved checkpoint as a local integrity pin.

The digest records byte identity for future runs. It is not a provider signature
or proof that the local file came from Meta.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import tempfile
from datetime import datetime, timezone
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
RISK_ROOT = PROJECT.parent / "jepa-risk-detection"
EXPECTED_CHECKPOINT = RISK_ROOT / "artifacts" / "checkpoints" / "vitl.pt"
EXPECTED_SIZE = 5_127_726_842
UPSTREAM = "https://github.com/facebookresearch/vjepa2/tree/204698b45b3712590f06245fbfba32d3be539812"


def digest_file(path: Path) -> tuple[str, os.stat_result]:
    before = path.stat()
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    after = path.stat()
    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
        after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns
    ):
        raise RuntimeError("checkpoint changed while its local identity was being registered")
    return digest.hexdigest(), after


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--checkpoint", required=True)
    parser.add_argument("--replace", action="store_true", help="replace an existing local pin after explicit review")
    args = parser.parse_args()

    supplied = Path(args.checkpoint).expanduser()
    if supplied.is_symlink():
        raise SystemExit("refusing a symlink checkpoint")
    checkpoint = supplied.resolve(strict=True)
    if checkpoint != EXPECTED_CHECKPOINT.resolve(strict=True):
        raise SystemExit("only the code-configured existing jepa-risk-detection checkpoint may be registered")
    if not checkpoint.is_file():
        raise SystemExit("configured local checkpoint is not a regular file")
    metadata = checkpoint.stat()
    if metadata.st_size != EXPECTED_SIZE:
        raise SystemExit(f"existing checkpoint size differs from its recorded local size ({metadata.st_size} bytes)")

    identity_path = PROJECT / ".local" / "checkpoint-identity.json"
    identity_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    identity_path.parent.chmod(0o700)
    if identity_path.exists() and not args.replace:
        raise SystemExit("local checkpoint identity already exists; refusing to replace it")

    digest, after = digest_file(checkpoint)
    record = {
        "schema_version": 1,
        "checkpoint_path": str(checkpoint),
        "checkpoint_bytes": after.st_size,
        "sha256": digest,
        "registered_at_utc": datetime.now(timezone.utc).isoformat(),
        "source": {
            "description": (
                "Pre-existing checkpoint at the code-configured path. The adjacent jepa-risk-detection README "
                "identifies the ViT-L checkpoint as the official Meta model and records this local size."
            ),
            "reference": str(RISK_ROOT / "README.md"),
            "upstream_reference": UPSTREAM,
            "provider_checksum_status": "no official digest or signature recorded; provider authenticity unverified",
            "registration_basis": "existing local file explicitly authorized for reuse by the user",
        },
        "integrity_scope": "local byte identity for subsequent runs; this SHA-256 is not a provider authenticity proof",
    }

    fd, tmp_name = tempfile.mkstemp(prefix="checkpoint-identity-", suffix=".tmp", dir=identity_path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(record, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(tmp_name, stat.S_IRUSR | stat.S_IWUSR)
        os.replace(tmp_name, identity_path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)

    print(json.dumps({
        "registered": True,
        "checkpoint_bytes": after.st_size,
        "sha256_prefix": digest[:16],
        "identity_file": str(identity_path),
        "provider_authentication": "unverified",
        "integrity_scope": "subsequent local bitwise consistency only",
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
