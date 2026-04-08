"""Auto-detect system labels and merge with user-supplied labels."""

import platform
import socket
import logging
import subprocess
from pathlib import Path

from topdown.collector.process_resolver import get_process_cmdline

logger = logging.getLogger(__name__)


def collect_auto_labels(
    process_name: str,
    pids: list[int],
    toplev_level: int,
    toplev_path: str = "toplev.py",
) -> dict[str, str]:
    """Collect all auto-detected system labels."""
    labels: dict[str, str] = {}

    # System
    labels["node"] = socket.gethostname()
    labels["kernel_version"] = platform.release()
    labels["arch"] = platform.machine()

    # CPU
    labels["cpu"] = _get_cpu_model()
    labels["pmu_name"] = _get_pmu_name()
    labels["platform"] = _detect_platform()

    # Process
    labels["comm"] = process_name
    labels["pid"] = ",".join(str(p) for p in pids)
    if pids:
        try:
            labels["cmdline"] = get_process_cmdline(pids[0])
        except (PermissionError, FileNotFoundError, ProcessLookupError):
            pass

    # Topdown
    labels["toplev_level"] = str(toplev_level)
    labels["pmu_tools_version"] = _get_toplev_version(toplev_path)

    return labels


def merge_labels(auto_labels: dict[str, str], user_labels: dict[str, str]) -> dict[str, str]:
    """Merge auto-detected and user-supplied labels. User labels take precedence."""
    merged = dict(auto_labels)
    merged.update(user_labels)
    return merged


def parse_label_args(label_args: list[str] | None) -> dict[str, str]:
    """Parse ['key1=val1', 'key2=val2'] to dict."""
    if not label_args:
        return {}
    labels = {}
    for arg in label_args:
        if "=" not in arg:
            raise ValueError(f"Invalid label format '{arg}', expected 'key=value'")
        key, value = arg.split("=", 1)
        labels[key.strip()] = value.strip()
    return labels


def _get_cpu_model() -> str:
    try:
        with open("/proc/cpuinfo", "r") as f:
            for line in f:
                if line.startswith("model name"):
                    return line.split(":", 1)[1].strip()
    except (FileNotFoundError, PermissionError):
        pass
    return platform.processor() or "unknown"


def _get_pmu_name() -> str:
    pmu_path = Path("/sys/devices/cpu/caps/pmu_name")
    try:
        return pmu_path.read_text().strip()
    except (FileNotFoundError, PermissionError):
        return "unknown"


def _detect_platform() -> str:
    """Try to detect cloud instance type."""
    # AWS
    try:
        result = subprocess.run(
            ["curl", "-s", "-m", "1", "http://169.254.169.254/latest/meta-data/instance-type"],
            capture_output=True,
            text=True,
            timeout=2,
        )
        if result.returncode == 0 and result.stdout.strip():
            arch = platform.machine()
            return f"{arch}-aws-{result.stdout.strip()}"
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass

    # GCP
    try:
        result = subprocess.run(
            [
                "curl",
                "-s",
                "-m",
                "1",
                "-H",
                "Metadata-Flavor: Google",
                "http://metadata.google.internal/computeMetadata/v1/instance/machine-type",
            ],
            capture_output=True,
            text=True,
            timeout=2,
        )
        if result.returncode == 0 and result.stdout.strip():
            machine_type = result.stdout.strip().split("/")[-1]
            arch = platform.machine()
            return f"{arch}-gcp-{machine_type}"
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass

    return f"{platform.machine()}-{platform.node()}"


def _get_toplev_version(toplev_path: str) -> str:
    try:
        result = subprocess.run(
            [toplev_path, "--version"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip() or result.stderr.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return "unknown"
