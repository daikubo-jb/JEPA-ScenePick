from __future__ import annotations

import json
import io
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import scale_benchmark as benchmark
import scale_diagnose as diagnose
import scale_retest as retest


PRIVATE_MARKER = "/Users/synthetic-user/Movies/PRIVATE-ERROR-MARKER/game.mp4"


def _fail_with_private_path():
    raise RuntimeError(f"synthetic failure at {PRIVATE_MARKER}")


def test_benchmark_worker_progress_stdout_contains_aggregate_only(capsys):
    benchmark.emit_stage({
        "dataset": "synthetic-fixture",
        "engine": "color-motion",
        "video_count": 1,
        "windows_in_store": 2,
        "search_wall_seconds": 0.1,
        "store_bytes": 12,
        "process_peak_rss_bytes": 34,
        "devices": ["cpu"],
        "media_error_count": 1,
        "media_errors_local_only": [{"message": PRIVATE_MARKER}],
        "per_video_local_only": [{"path": PRIVATE_MARKER}],
    })
    output = capsys.readouterr()
    assert PRIVATE_MARKER not in output.out
    assert PRIVATE_MARKER not in output.err
    assert json.loads(output.out)["media_error_count"] == 1


def test_benchmark_parent_filters_untrusted_worker_stdout_and_stderr(tmp_path, monkeypatch, capsys):
    local = tmp_path / "state"
    private = local / "private"
    private.mkdir(parents=True)
    safe = {
        "stage_complete": "synthetic-smoke", "engine": "color-motion", "video_count": 1,
        "media_error_count": 1, "windows": 2, "index_seconds": 0.25,
        "search_seconds": 0.1, "store_bytes": 12, "peak_rss_bytes": 34, "devices": ["cpu"],
    }
    unsafe = {**safe, "exception": PRIVATE_MARKER}

    class FailedWorker:
        stdout = io.StringIO(json.dumps(unsafe) + "\n" + json.dumps(safe) + "\n" + f"failure {PRIVATE_MARKER}\n")
        stderr = io.StringIO(f"traceback {PRIVATE_MARKER}\n")

        @staticmethod
        def wait():
            return 1

    monkeypatch.setattr(benchmark, "LOCAL", local)
    monkeypatch.setattr(benchmark, "runtime_record", lambda: {"model_available": True})
    monkeypatch.setattr(benchmark.subprocess, "Popen", lambda *args, **kwargs: FailedWorker())
    assert benchmark.benchmark() == 1
    output = capsys.readouterr()
    assert PRIVATE_MARKER not in output.out + output.err
    assert json.loads(output.out.splitlines()[1]) == safe
    assert json.loads(output.out.splitlines()[-1])["status"] == "failed"


def test_benchmark_cli_suppresses_exception_text_in_stdio_and_error_record(tmp_path, monkeypatch, capsys):
    local = tmp_path / "state"
    (local / "private").mkdir(parents=True)
    monkeypatch.setattr(benchmark, "LOCAL", local)
    monkeypatch.setattr(benchmark, "load_config", _fail_with_private_path)
    monkeypatch.setattr(sys, "argv", ["scale_benchmark.py", "--stage", "inventory"])
    assert benchmark.main() == 2
    output = capsys.readouterr()
    record = (local / "private" / "last-error.json").read_text(encoding="utf-8")
    assert PRIVATE_MARKER not in output.out + output.err + record
    assert json.loads(output.err)["error_class"] == "RuntimeError"


def test_diagnose_cli_suppresses_exception_text_in_stdio_and_error_record(tmp_path, monkeypatch, capsys):
    private = tmp_path / "state" / "private" / "diagnostics"
    monkeypatch.setattr(diagnose, "PRIVATE", private)
    monkeypatch.setattr(diagnose, "cfg", _fail_with_private_path)
    monkeypatch.setattr(sys, "argv", ["scale_diagnose.py", "--stage", "diagnose"])
    assert diagnose.cli_main() == 2
    output = capsys.readouterr()
    record = (private / "diagnostic-error.json").read_text(encoding="utf-8")
    assert PRIVATE_MARKER not in output.out + output.err + record
    assert json.loads(output.err)["error_class"] == "RuntimeError"


def test_retest_cli_suppresses_exception_text_in_stdio(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(retest.source, "load_config", _fail_with_private_path)
    assert retest.cli_main() == 2
    output = capsys.readouterr()
    assert PRIVATE_MARKER not in output.out + output.err
    assert json.loads(output.err)["error_class"] == "RuntimeError"
