import math
import os
from pathlib import Path
import re
import subprocess
import sys

import pytest


SOURCE = Path(__file__).resolve().parents[1] / "examples" / "apple_gpu" / "probe.swift"
XCODE = Path("/Applications/Xcode.app/Contents/Developer")


@pytest.mark.skipif(sys.platform != "darwin" or not XCODE.is_dir(), reason="requires full Xcode on macOS")
def test_metal_probe_alu_and_stream_workloads(tmp_path):
    environment = os.environ.copy()
    environment["DEVELOPER_DIR"] = str(XCODE)
    executable = tmp_path / "probe"
    subprocess.run(
        ["xcrun", "swiftc", "-O", "-framework", "Metal", str(SOURCE), "-o", str(executable)],
        check=True,
        capture_output=True,
        timeout=60,
        env=environment,
    )
    for workload, expected in (("alu", 8336.906823405065), ("stream", 822083536.0)):
        result = subprocess.run(
            [str(executable), "4" if workload == "alu" else "2", workload],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
            env=environment,
        )
        match = re.search(rf"workload={workload} passes=\d+ items=\d+ gpu_seconds=([0-9.e-]+) checksum=([0-9.e-]+)",
                          result.stdout)
        assert match, result.stdout
        assert float(match.group(1)) > 0
        assert math.isclose(float(match.group(2)), expected, rel_tol=1e-6)
        assert "checksum_verified=true" in result.stdout

    for arguments in (["garbage"], ["0"], ["501"], ["4", "unknown"], ["4", "alu", "extra"]):
        result = subprocess.run([str(executable), *arguments], capture_output=True, text=True, timeout=10)
        assert result.returncode != 0
        assert "Error:" in result.stderr
        assert "device=" not in result.stdout
