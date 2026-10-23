"""Dependent-read evidence keeps logical work, timing and unavailable limiters distinct."""

import hashlib
import json
from pathlib import Path
import platform
import shutil
import subprocess

import pytest

from topdown.apple_gpu_report import render, summarize
from topdown.apple_storage import query_apple_result, store_apple_result, stored_apple_gpu_report

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = json.loads((ROOT / "docs/apple-gpu-m6-dependent-reads.json").read_text())


def test_dependent_read_sources_and_balanced_replication_match_recorded_controls():
    assert hashlib.sha256((ROOT / EVIDENCE["source"]).read_bytes()).hexdigest() == EVIDENCE["source_sha256"]
    replication = EVIDENCE["public_runner_replication"]
    assert hashlib.sha256((ROOT / replication["source"]).read_bytes()).hexdigest() == replication["source_sha256"]
    assert replication["order"] == EVIDENCE["order"]
    assert EVIDENCE["order"][:6] == list(reversed(EVIDENCE["order"][6:]))
    assert len(EVIDENCE["cases"]) == len(replication["cases"]) == 12


@pytest.mark.parametrize("case", EVIDENCE["cases"] + EVIDENCE["public_runner_replication"]["cases"],
                         ids=lambda case: str(case["trial"]))
def test_equal_logical_reads_keep_verified_outputs_and_native_counter_availability(case):
    probe = case["probe"]
    assert probe["checksum_verified"] and probe["elements_verified"]
    assert probe["checksum"] == probe["expected_checksum"]
    assert probe["dependent_reads"] == probe["steps"] * probe["threads"] == 16777216
    assert probe["table_bytes"] == 4 * (1 << probe["table_bits"])
    profile = case.get("profile")
    if profile is None:
        baseline = EVIDENCE["cases"][case["trial"]]["profile"]
        profile = {**baseline, **{key: case[key] for key in ("timeline", "encoders", "timeline_counter_groups")}}
    report = summarize(profile)
    assert report["total_encoders"] == 1 and report["encoders"][0]["cost_percent"] == 100
    assert report["timeline_counter_scope"]["target_exclusive"] is False
    if probe["threads"] == 64:
        assert report["encoders"][0]["limiters"] == []
        assert "Native limiter data unavailable" in render(report)
    metrics = {row["id"]: row for row in report["timeline_metrics"]}
    assert metrics["gpu_bandwidth"]["native_value"].endswith(" GiB / Second")
    assert metrics["active_cores"]["observed"]


def check_measured_dependent_read_storage(backend, tmp_path):
    for case in EVIDENCE["cases"]:
        original = case["profile"]
        path = tmp_path / "profile.json"
        path.write_text(json.dumps(original))
        run = store_apple_result(backend, path, process_name="dependent-reads")
        assert backend.get_apple_result(run.run_id)["result"] == original
        assert query_apple_result(backend, run.run_id, "encoders", 0, 1)["rows"] == original["encoders"]
        assert stored_apple_gpu_report(backend, run.run_id) == {"run_id": run.run_id, **summarize(original, 20)}


def test_sqlite_dependent_read_native_profiles(sqlite_backend, tmp_path):
    check_measured_dependent_read_storage(sqlite_backend, tmp_path)


@pytest.mark.skipif(platform.system() != "Darwin" or platform.machine() != "arm64" or not shutil.which("xcrun"),
                    reason="requires native Apple silicon compiler")
def test_dependent_read_probe_rejects_invalid_configuration_before_gpu_work(tmp_path):
    binary = tmp_path / "probe"
    subprocess.run(["xcrun", "swiftc", "-O", "-framework", "Metal", str(ROOT / EVIDENCE["source"]), "-o", str(binary)],
                   capture_output=True, check=True, timeout=90)
    for args in ([], ["1", "64", str(tmp_path)], ["24", "1", str(tmp_path)], ["24", "64", str(tmp_path / "missing")]):
        result = subprocess.run([str(binary), *args], text=True, capture_output=True, timeout=3)
        assert result.returncode == 1 and "Error:" in result.stderr
    assert not (tmp_path / "capture.gputrace").exists() and not (tmp_path / "probe.json").exists()
