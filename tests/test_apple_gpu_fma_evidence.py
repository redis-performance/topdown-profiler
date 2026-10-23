"""Measured FMA controls retain native units, missing counters and replay scope."""

import hashlib
import json
from pathlib import Path

import pytest

from topdown.apple_gpu_report import summarize
from topdown.apple_storage import query_apple_result, store_apple_result, stored_apple_gpu_report

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = json.loads((ROOT / "docs/apple-gpu-m6-fma-chains.json").read_text())
CASES = EVIDENCE["cases"] + EVIDENCE["public_runner_replication"]["cases"]


def test_measured_fma_sources_and_replication_orders_match():
    replication = EVIDENCE["public_runner_replication"]
    for run in (EVIDENCE, replication):
        assert hashlib.sha256((ROOT / run["source"]).read_bytes()).hexdigest() == run["source_sha256"]
        assert len(run["cases"]) == 12
        assert run["order"][:6] == list(reversed(run["order"][6:]))
    assert replication["order"] == EVIDENCE["order"]
    assert replication["probe_source_sha256"] == EVIDENCE["source_sha256"]


@pytest.mark.parametrize("case", CASES, ids=lambda case: str(case["trial"]))
def test_measured_fma_work_and_native_availability_remain_distinct(case):
    probe, profile = case["probe"], case["profile"]
    assert probe["elements_bitwise_verified"] and probe["checksum_verified"]
    assert probe["checksum"] == probe["expected_checksum"]
    assert probe["threads"] * probe["chains_per_thread"] * probe["steps"] == probe["logical_fmas"] == 268435456
    assert probe["elements"] == probe["threads"] * probe["chains_per_thread"]
    assert probe["input_output_bytes"] == probe["elements"] * 8
    assert probe["fast_math_enabled"] is False
    for key in ("probe", "profile"):
        canonical = json.dumps(case[key], sort_keys=True, separators=(",", ":")).encode()
        assert hashlib.sha256(canonical).hexdigest() == case[key + "_canonical_sha256"]
    report = summarize(profile)
    assert report["total_encoders"] == 1 and report["encoders"][0]["cost_percent"] == 100
    assert report["timeline_counter_scope"]["target_exclusive"] is False
    f32 = {row["id"]: row for row in report["timeline_metrics"] if row["group"] == "f32"}
    assert f32["f32_utilization"]["observed"]
    assert f32["f32_utilization"]["native_value"].endswith(" Percentage of Peak F32 Performance")
    if probe["threads"] == 64 and probe["chains_per_thread"] == 1:
        assert "F32 Limiter" not in report["encoders"][0]["native_counters"]
        assert all(row["name"] != "F32 Limiter" for row in report["encoders"][0]["limiters"])


def check_measured_fma_storage(backend, tmp_path):
    for case in CASES:
        original = case["profile"]
        path = tmp_path / "profile.json"
        path.write_text(json.dumps(original))
        run = store_apple_result(backend, path, process_name="fma-chains", labels={
            "threads": str(case["probe"]["threads"]), "chains_per_thread": str(case["probe"]["chains_per_thread"]),
            "logical_fmas": str(case["probe"]["logical_fmas"]), "source_sha256": EVIDENCE["source_sha256"]})
        assert backend.get_apple_result(run.run_id)["result"] == original
        assert query_apple_result(backend, run.run_id, "encoders", 0, 1)["rows"] == original["encoders"]
        assert stored_apple_gpu_report(backend, run.run_id) == {"run_id": run.run_id, **summarize(original, 20)}


def test_sqlite_keeps_measured_fma_replays_exact(sqlite_backend, tmp_path):
    check_measured_fma_storage(sqlite_backend, tmp_path)
