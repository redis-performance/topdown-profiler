"""Recorded counted-loop snapshots preserve exact native accounting through storage."""

import hashlib
import json
from pathlib import Path
import platform
import shutil
import subprocess

import pytest

from topdown.apple_storage import query_apple_result, store_apple_result, validate_result
from topdown.collector.apple import TMA_ROOTS

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = json.loads((ROOT / "docs/apple-m6-fixed-instruction-validation.json").read_text())


def native_result(case, window):
    return {"measurement_kind": "process-accounted-fixed-counters", "tma_available": False,
            "tma": dict.fromkeys(TMA_ROOTS), "source": EVIDENCE["counter_source"],
            "environment": EVIDENCE["environment"], **case[window]}


def test_counted_loop_source_and_validation_script_match_the_native_measurements():
    assert hashlib.sha256((ROOT / EVIDENCE["source"]).read_bytes()).hexdigest() == EVIDENCE["source_sha256"]
    assert hashlib.sha256((ROOT / EVIDENCE["validation_script"]).read_bytes()).hexdigest() == EVIDENCE["validation_script_sha256"]
    assert len(EVIDENCE["cases"]) == len(EVIDENCE["order"]) * 2 == 48
    assert {case["qos"] for case in EVIDENCE["cases"]} == {"default", "background"}


@pytest.mark.parametrize("case", EVIDENCE["cases"], ids=lambda case: f"{case['trial']}-{case['qos']}")
def test_measured_loop_counts_and_native_mach_quantization(case):
    iterations = EVIDENCE["order"][case["trial"]]
    assert case["iterations"] == case["probe"]["iterations"] == case["probe"]["checksum"] == iterations
    assert case["probe"]["loop_instructions"] == iterations * 3
    for window in ("lifetime", "interval"):
        result = native_result(case, window)
        validate_result(result)
        counters = result["counters"]
        assert counters["instructions"] >= case["probe"]["loop_instructions"]
        assert counters["instructions"] == counters["performance_instructions"] + counters["other_instructions"]
        for stem in ("user_time", "system_time"):
            assert counters[stem + "_mach"] == counters["performance_" + stem + "_mach"] + counters["other_" + stem + "_mach"]
            difference = counters[stem + "_ns"] - counters["performance_" + stem + "_ns"] - counters["other_" + stem + "_ns"]
            assert abs(difference) <= 1


def check_measured_fixed_instruction_storage(backend, tmp_path):
    for case in EVIDENCE["cases"]:
        for window in ("lifetime", "interval"):
            original = native_result(case, window)
            path = tmp_path / "stat.json"
            path.write_text(json.dumps(original))
            run = store_apple_result(backend, path, process_name="counted-loop", labels={"qos": case["qos"]})
            assert backend.get_apple_result(run.run_id)["result"] == original
            assert query_apple_result(backend, run.run_id)["result"] == original
            assert run.labels["cpu"] == "Apple M6" and run.labels["time_basis"] == "result_timestamps"


def test_sqlite_measured_fixed_instruction_snapshots(sqlite_backend, tmp_path):
    check_measured_fixed_instruction_storage(sqlite_backend, tmp_path)


@pytest.mark.skipif(platform.system() != "Darwin" or platform.machine() != "arm64" or not shutil.which("xcrun"),
                    reason="requires native Apple silicon compiler")
def test_native_counted_loop_rejects_bad_arguments_and_checks_the_loop(tmp_path):
    binary = tmp_path / "counted-loop"
    subprocess.run(["xcrun", "clang", "-O2", "-arch", "arm64", str(ROOT / EVIDENCE["source"]), "-o", str(binary)],
                   capture_output=True, check=True, timeout=60)
    for args in ([], ["", "default"], ["bad", "default"], ["-1", "default"], ["100000001", "default"],
                 ["1", "unknown"], ["1", "default", "--wrong"]):
        response = subprocess.run([str(binary), *args], capture_output=True, text=True, timeout=2, stdin=subprocess.DEVNULL)
        assert response.returncode == 2
    for iterations in (0, 1000):
        response = subprocess.run([str(binary), str(iterations), "default"], capture_output=True,
                                  text=True, check=True, timeout=2, stdin=subprocess.DEVNULL)
        assert json.loads(response.stdout) == {"iterations": iterations, "checksum": iterations,
                                              "loop_instructions": iterations * 3}
