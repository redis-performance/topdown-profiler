"""Recognize the measured M6 kernel without guessing about other kernels or CPUs."""

import json
import subprocess
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from topdown.cli import app
from topdown.collector.apple import M6_BLOCKED_KERNEL_UUID, doctor


def host_doctor(cpu="Apple M6", uuid=M6_BLOCKED_KERNEL_UUID, fail_uuid=False, gpu_error=None,
                xcode_version="27.1"):
    outputs = {
        ("sysctl", "-n", "machdep.cpu.brand_string"): cpu,
        ("sysctl", "-n", "kern.osversion"): "26A434",
        ("sysctl", "-n", "kern.uuid"): uuid,
        ("xcrun", "xctrace", "version"): f"xctrace version {xcode_version}",
        ("xcrun", "gpudebug", "--version"): "gpudebug 1.0",
        ("xcrun", "xctrace", "list", "templates"): "CPU Counters\nCPU Profiler\nTime Profiler\nProcessor Trace",
        ("/usr/sbin/DevToolsSecurity", "-status"): "Developer mode is currently enabled.",
    }

    def fake(cmd, **kwargs):
        if cmd == ["xcrun", "gpudebug", "--version"] and gpu_error is not None:
            if isinstance(gpu_error, Exception):
                raise gpu_error
            return subprocess.CompletedProcess(cmd, 1, "", gpu_error)
        if cmd[-1] == "kern.uuid" and fail_uuid:
            return subprocess.CompletedProcess(cmd, 1, "", "kernel UUID unavailable")
        return subprocess.CompletedProcess(cmd, 0, outputs[tuple(cmd)], "")

    with patch("platform.system", return_value="Darwin"), patch("platform.machine", return_value="arm64"), \
         patch("subprocess.run", side_effect=fake):
        return doctor()


def test_known_kernel_identifies_blocked_modes_and_gives_working_capture_path():
    result = host_doctor(uuid=M6_BLOCKED_KERNEL_UUID.lower())
    diagnostic = result["programmable_counters"]
    assert diagnostic["status"] == "known_kernel_blocker"
    assert diagnostic["affected_modes"] == ["counters", "cpu"]
    assert "EPERM" in diagnostic["reason"] and "Xcode 27.0 and 27.1" in diagnostic["reason"]
    assert "--mode fixed" in diagnostic["next_step"] and "apple report" in diagnostic["next_step"]
    assert result["hardware_access"] == "unverified"
    assert result["os_build"] == "26A434"
    assert result["gpudebug"] == "gpudebug 1.0"
    assert result["gpu_replay"]["status"] == "unverified"
    assert "apple gpu record" in result["gpu_replay"]["next_step"]


@pytest.mark.parametrize("cpu,uuid,fail_uuid", [
    ("Apple M6", "FUTURE-KERNEL", False), ("Apple M1", M6_BLOCKED_KERNEL_UUID, False),
    ("Apple M6", M6_BLOCKED_KERNEL_UUID, True),
])
def test_other_hosts_and_missing_metadata_remain_unverified(cpu, uuid, fail_uuid):
    result = host_doctor(cpu, uuid, fail_uuid)
    assert result["programmable_counters"]["status"] == "unverified"
    assert "probe --mode counters" in result["programmable_counters"]["next_step"]
    if fail_uuid:
        assert "kernel UUID unavailable" in result["errors"]["kernel_uuid"]


@pytest.mark.parametrize("cpu", ["Apple M1", "Apple M1 Max", "Apple M2", "Apple M2 Ultra"])
def test_doctor_routes_pre_m3_gpu_generations_to_instruments(cpu):
    result = host_doctor(cpu=cpu, uuid="OTHER-KERNEL")
    assert result["gpu_replay"]["status"] == "known_gpu_generation_limit"
    assert "M3 / A17 Pro" in result["gpu_replay"]["reason"]
    assert "record_instruments.py" in result["gpu_replay"]["next_step"]
    assert result["programmable_counters"]["status"] == "unverified"
    assert host_doctor(cpu="Apple M10", uuid="OTHER-KERNEL")["gpu_replay"]["status"] == "unverified"
    assert host_doctor(cpu=cpu, uuid="OTHER-KERNEL", xcode_version="28")[
        "gpu_replay"]["status"] == "unverified"


@pytest.mark.parametrize("version", ["27.10", "127.1", "27.1beta", "27.1.1"])
def test_known_m1_gpu_limit_does_not_match_unverified_tool_versions_by_substring(version):
    assert host_doctor(cpu="Apple M1", uuid="OTHER-KERNEL", xcode_version=version)["gpu_replay"]["status"] == "unverified"


def test_doctor_cli_retains_diagnostic_without_disabling_other_modes():
    report = host_doctor()
    with patch("topdown.apple_cli.doctor", return_value=report):
        result = CliRunner().invoke(app, ["apple", "doctor"])
    assert result.exit_code == 0
    assert json.loads(result.output)["programmable_counters"]["status"] == "known_kernel_blocker"


def test_timeout_retains_unknown_access_instead_of_guessing():
    with patch("platform.system", return_value="Darwin"), patch("platform.machine", return_value="arm64"), \
         patch("subprocess.run", side_effect=subprocess.TimeoutExpired("sysctl", 10)):
        result = doctor()
    assert result["programmable_counters"]["status"] == "unverified"
    assert "kernel_uuid" in result["errors"]


@pytest.mark.parametrize("error", [
    "GPU debugger unavailable", FileNotFoundError("xcrun missing"), subprocess.TimeoutExpired("gpudebug", 10),
])
def test_gpu_metadata_failure_does_not_change_cpu_or_claim_hardware_availability(error):
    result = host_doctor(gpu_error=error)
    assert result["gpudebug"] is None
    assert result["errors"]["gpudebug"]
    assert result["gpu_replay"]["status"] == "unverified"
    assert result["programmable_counters"]["status"] == "known_kernel_blocker"
    assert result["hardware_access"] == "unverified"


def test_unsupported_host_has_no_gpu_replay_probe():
    with patch("platform.system", return_value="Linux"), patch("subprocess.run") as run:
        result = doctor()
    assert result["gpu_replay"]["status"] == "unsupported_host"
    assert result["gpudebug"] is None
    run.assert_not_called()
