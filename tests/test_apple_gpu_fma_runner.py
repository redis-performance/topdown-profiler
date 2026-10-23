"""The FMA control verifies equal logical work before replaying native captures."""

import hashlib
import importlib.util
import json
from pathlib import Path
import platform
import shutil
import struct
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "examples/apple_gpu/fma_chains.py"


@pytest.fixture
def runner():
    spec = importlib.util.spec_from_file_location("fma_runner", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def probe(threads=64, chains=1):
    return {"threads": threads, "chains_per_thread": chains, "elements": threads * chains,
            "steps": 268435456 // (threads * chains), "logical_fmas": 268435456,
            "input_output_bytes": threads * chains * 8, "multiplier_bits": 0x3f800001,
            "addend_bits": [struct.unpack("<I", struct.pack("<f", (index + 1) * 2**-24))[0]
                            for index in range(chains)], "device": "test device", "gpu_seconds": .1,
            "fast_math_enabled": False, "checksum_verified": True, "elements_bitwise_verified": True,
            "checksum": 123, "expected_checksum": 123}


@pytest.mark.parametrize("threads,chains", [(64, 1), (64, 4), (64, 16), (4096, 1), (4096, 4), (4096, 16)])
def test_runner_verifies_fixed_fmas_and_explicit_float_coefficients(runner, threads, chains):
    runner._verify_probe(probe(threads, chains), threads, chains)


@pytest.mark.parametrize("field,value", [
    ("threads", True), ("threads", 64.0), ("chains_per_thread", True), ("elements", 1),
    ("steps", 1), ("logical_fmas", 16777216), ("input_output_bytes", 1), ("multiplier_bits", 0),
    ("addend_bits", [True]), ("addend_bits", [float(0x33800000)]), ("addend_bits", []),
    ("fast_math_enabled", 0), ("fast_math_enabled", True), ("checksum_verified", 1),
    ("elements_bitwise_verified", False), ("checksum", 122), ("checksum", True),
    ("expected_checksum", 123.0), ("device", " "), ("gpu_seconds", True),
    ("gpu_seconds", 0), ("gpu_seconds", float("nan")), ("gpu_seconds", float("inf")),
    pytest.param("gpu_seconds", 2**10000, id="oversized-int-seconds"),
])
def test_bad_verification_cannot_be_presented_as_measured_work(runner, field, value):
    with pytest.raises(ValueError, match="verification failed"):
        runner._verify_probe({**probe(), field: value}, 64, 1)


@pytest.mark.parametrize("value", [None, [], {}, {"threads": 64}])
def test_incomplete_probe_rejected(runner, value):
    with pytest.raises(ValueError, match="verification failed"):
        runner._verify_probe(value, 64, 1)


@pytest.mark.parametrize("invalid", [False, True])
def test_runner_keeps_balanced_order_and_frozen_source_provenance(tmp_path, runner, monkeypatch, invalid):
    monkeypatch.setattr(runner, "_require_host", lambda: None)
    replayed = []

    def run(output, name, command, timeout, env=None):
        if name == "compile":
            assert Path(command[5]).name == "probe.swift"
            Path(command[-1]).write_bytes(b"test binary")
        else:
            assert env["MTL_CAPTURE_ENABLED"] == "1"
            threads, chains = int(command[1]), int(command[2])
            data = probe(threads, chains)
            if invalid:
                data["elements_bitwise_verified"] = False
            (output / "probe.json").write_text(json.dumps(data))

    def collect(trace, output):
        replayed.append(trace.parent.name)
        return {"timeline": {"GPU time": "1 ms"}}

    monkeypatch.setattr(runner, "_run", run)
    monkeypatch.setattr(runner, "collect", collect)
    output = tmp_path / "run"
    if invalid:
        with pytest.raises(ValueError, match="verification failed"):
            runner.experiment(output)
        assert not replayed and not (output / "results.json").exists()
        assert list(output.glob("trial-*/probe.json"))
    else:
        runner.experiment(output)
        evidence = json.loads((output / "results.json").read_text())
        assert len(evidence["cases"]) == len(replayed) == 12
        assert evidence["order"][:6] == list(reversed(evidence["order"][6:]))
        assert evidence["source_sha256"] == hashlib.sha256((output / "probe.swift").read_bytes()).hexdigest()
        assert evidence["runner_sha256"] == hashlib.sha256((output / "runner.py").read_bytes()).hexdigest()
        assert (output / "runner.py").read_bytes() == SCRIPT.read_bytes()


@pytest.mark.parametrize("outcome", ["ok", "failed", "timeout"])
def test_native_tool_logs_survive_failures(tmp_path, runner, monkeypatch, outcome):
    def run(command, **kwargs):
        assert kwargs["stdin"] == subprocess.DEVNULL
        if outcome == "timeout":
            raise subprocess.TimeoutExpired(command, 1, output=b"partial output", stderr=b"partial error")
        return subprocess.CompletedProcess(command, 1 if outcome == "failed" else 0, "partial output", "partial error")

    monkeypatch.setattr(subprocess, "run", run)
    if outcome == "ok":
        runner._run(tmp_path, "capture", ["test native tool"], 1)
    else:
        with pytest.raises(ValueError, match="artifacts"):
            runner._run(tmp_path, "capture", ["test native tool"], 1)
    assert (tmp_path / "capture.stdout").read_text() == "partial output"
    assert (tmp_path / "capture.stderr").read_text() == "partial error"
    assert json.loads((tmp_path / "capture.command.json").read_text()) == ["test native tool"]


@pytest.mark.skipif(platform.system() != "Darwin" or platform.machine() != "arm64" or not shutil.which("xcrun"),
                    reason="requires native Apple silicon compiler")
def test_fma_probe_rejects_invalid_configuration_before_gpu_work(tmp_path):
    binary = tmp_path / "probe"
    subprocess.run(["xcrun", "swiftc", "-O", "-framework", "Metal",
                    str(SCRIPT.with_name("fma_chains_probe.swift")), "-o", str(binary)],
                   capture_output=True, check=True, timeout=90)
    for args in ([], ["1", "1", str(tmp_path)], ["64", "2", str(tmp_path)], ["64", "1", str(tmp_path / "missing")]):
        result = subprocess.run([str(binary), *args], text=True, capture_output=True, timeout=3)
        assert result.returncode == 1 and "Error:" in result.stderr
    assert not (tmp_path / "capture.gputrace").exists() and not (tmp_path / "probe.json").exists()
