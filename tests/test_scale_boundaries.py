from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import scale_benchmark as scale


def test_public_manifest_accepts_in_root_synthetic_file(tmp_path, monkeypatch):
    local = tmp_path / "local"
    public = local / "public"
    public.mkdir(parents=True)
    (public / "sample.mp4").write_bytes(b"synthetic placeholder")
    monkeypatch.setattr(scale, "LOCAL", local)
    rows = scale.public_files({
        "public_source_root": str(public),
        "public_manifest": [{"file": "sample.mp4", "label": "synthetic"}],
    })
    assert rows[0][0] == (public / "sample.mp4").resolve()


@pytest.mark.parametrize("entry", ["/tmp/outside.mp4", "../outside.mp4", "nested/../../outside.mp4"])
def test_public_manifest_rejects_absolute_and_parent_traversal(tmp_path, monkeypatch, entry):
    local = tmp_path / "local"
    public = local / "public"
    public.mkdir(parents=True)
    (tmp_path / "outside.mp4").write_bytes(b"synthetic placeholder")
    monkeypatch.setattr(scale, "LOCAL", local)
    with pytest.raises(RuntimeError, match="approved root"):
        scale.public_files({"public_source_root": str(public), "public_manifest": [{"file": entry}]})


def test_public_manifest_rejects_parent_symlink_escape(tmp_path, monkeypatch):
    local = tmp_path / "local"
    public = local / "public"
    outside = tmp_path / "outside"
    public.mkdir(parents=True)
    outside.mkdir()
    (outside / "sample.mp4").write_bytes(b"synthetic placeholder")
    (public / "linked").symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr(scale, "LOCAL", local)
    with pytest.raises(RuntimeError, match="approved root"):
        scale.public_files({"public_source_root": str(public), "public_manifest": [{"file": "linked/sample.mp4"}]})


def test_private_source_must_stay_under_local_private_root(tmp_path, monkeypatch):
    local = tmp_path / "local"
    private = local / "private" / "samples"
    private.mkdir(parents=True)
    for index in range(2):
        (private / f"synthetic-{index}.mp4").write_bytes(b"fixture")
    monkeypatch.setattr(scale, "LOCAL", local)
    assert len(scale.private_files({"private_source_root": str(private), "private_expected_count": 2})) == 2
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "synthetic.mp4").write_bytes(b"fixture")
    with pytest.raises(RuntimeError, match="unavailable or symlinked"):
        scale.private_files({"private_source_root": str(outside), "private_expected_count": 1})


def test_private_source_rejects_parent_symlink_even_when_target_is_in_scope(tmp_path, monkeypatch):
    local = tmp_path / "local"
    private = local / "private"
    samples = private / "samples"
    samples.mkdir(parents=True)
    (samples / "synthetic.mp4").write_bytes(b"fixture")
    (private / "alias").symlink_to(samples, target_is_directory=True)
    monkeypatch.setattr(scale, "LOCAL", local)
    with pytest.raises(RuntimeError, match="unavailable or symlinked"):
        scale.private_files({"private_source_root": str(private / "alias"), "private_expected_count": 1})


def test_load_config_rejects_private_root_outside_approved_root(tmp_path, monkeypatch):
    local = tmp_path / "local"
    (local / "private").mkdir(parents=True)
    public = local / "public"
    public.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    config = tmp_path / "config.json"
    config.write_text(json.dumps({
        "local_state_root": str(local), "public_source_root": str(public),
        "private_source_root": str(outside),
    }), encoding="utf-8")
    monkeypatch.setattr(scale, "LOCAL", local)
    monkeypatch.setattr(scale, "CONFIG", config)
    with pytest.raises(RuntimeError, match="config boundary"):
        scale.load_config()
