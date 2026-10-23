"""Run the bounded Metal controls where an Apple GPU is available."""

import json
import os
from pathlib import Path
import platform
import shutil
import subprocess

import pytest

pytestmark = pytest.mark.skipif(platform.system() != "Darwin" or platform.machine() != "arm64" or not shutil.which("xcrun"),
                                reason="Requires Apple silicon and Xcode")
SOURCE = Path(__file__).resolve().parents[1] / "topdown/collector/apple_gpu_probe.swift"


@pytest.fixture(scope="module")
def probe(tmp_path_factory):
    binary = tmp_path_factory.mktemp("metal-controls") / "probe"
    subprocess.run(["xcrun", "swiftc", "-O", "-framework", "Metal", str(SOURCE), "-o", str(binary)],
                   capture_output=True, text=True, check=True, timeout=60)
    return binary


@pytest.mark.parametrize("mode", ["compute", "alu", "memory", "stream", "idle"])
def test_gpu_controls_match_cpu_reference_with_partial_threadgroups(probe, tmp_path, mode):
    path = tmp_path / "result.json"
    subprocess.run([str(probe), "4", "--mode", mode, "--items", "192" if mode == "stream" else "129", "--result", str(path)],
                   text=True, capture_output=True, check=True, timeout=20)
    result = json.loads(path.read_text())
    assert result["checksum_verified"] and result["mode"] == mode
    assert result["passes"] == len(result["commands"]) == (0 if mode == "idle" else 4)
    assert (result["gpu_seconds"] == 0) == (mode == "idle")


def test_original_probe_checksum_remains_comparable_with_m1(probe, tmp_path):
    result = tmp_path / "result.json"
    subprocess.run([str(probe), "4", "--result", str(result)], text=True, capture_output=True, check=True, timeout=20)
    assert json.loads(result.read_text())["checksum"] == pytest.approx(8336.906823405065, abs=1e-7)


@pytest.mark.parametrize("args", [["0"], ["--mode", "invalid"], ["--items", "16777217"],
                                 ["500", "--items", "16777216"], ["--mode"], ["--unknown", "1"]])
def test_invalid_controls_fail_before_gpu_work(probe, args):
    result = subprocess.run([str(probe), *args], text=True, capture_output=True, timeout=10)
    assert result.returncode != 0 and "Error:" in result.stderr and "device=" not in result.stdout


def test_programmatic_capture_creates_only_a_new_trace(probe, tmp_path):
    trace = tmp_path / "probe.gputrace"
    env = {**os.environ, "MTL_CAPTURE_ENABLED": "1"}
    command = [str(probe), "1", "--capture", str(trace)]
    subprocess.run(command, env=env, text=True, capture_output=True, check=True, timeout=30)
    assert trace.exists()
    result = subprocess.run(command, env=env, text=True, capture_output=True, timeout=10)
    assert result.returncode != 0 and "new .gputrace path" in result.stderr
