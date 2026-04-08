"""Subprocess wrapper for toplev.py."""

import logging
import signal
import subprocess
import time
from dataclasses import dataclass, field

from topdown.collector.csv_parser import ToplevSample, parse_output

logger = logging.getLogger(__name__)


@dataclass
class ToplevOptions:
    level: int = 2
    interval_ms: int = 1000
    pids: list[int] | None = None
    system_wide: bool = False
    extra_args: list[str] = field(default_factory=list)


class ToplevRunner:
    def __init__(self, toplev_path: str, options: ToplevOptions):
        self.toplev_path = toplev_path
        self.options = options

    def build_command(self) -> list[str]:
        """Build the toplev command line."""
        cmd = [
            self.toplev_path,
            f"-l{self.options.level}",
            f"-I{self.options.interval_ms}",
            "-x,",  # CSV output with comma delimiter
            "--no-desc",  # Skip metric descriptions in output
        ]

        if self.options.pids:
            pid_str = ",".join(str(p) for p in self.options.pids)
            cmd.extend(["--pid", pid_str])
        elif self.options.system_wide:
            cmd.append("-a")

        cmd.extend(self.options.extra_args)
        return cmd

    def run(self, duration_seconds: int) -> tuple[str, str]:
        """Run toplev for a duration, return (stdout, stderr).

        toplev needs extra time beyond the collection duration for:
        - Initial PMU event list download (first run on a new CPU)
        - perf stat startup and calibration
        - Output flushing after SIGINT

        We use duration + 60s as the timeout buffer to handle slow starts
        on many-core systems (96+ cores on Sapphire Rapids, etc.).
        """
        cmd = self.build_command()
        logger.info("Running: %s (duration=%ds)", " ".join(cmd), duration_seconds)

        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

            try:
                stdout, stderr = proc.communicate(timeout=duration_seconds + 60)
            except subprocess.TimeoutExpired:
                proc.send_signal(signal.SIGINT)
                try:
                    stdout, stderr = proc.communicate(timeout=15)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    stdout, stderr = proc.communicate(timeout=5)

            # toplev uses SIGINT to stop collection normally
            time.sleep(0.5)
            if proc.poll() is None:
                proc.send_signal(signal.SIGINT)
                try:
                    stdout, stderr = proc.communicate(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    stdout, stderr = proc.communicate(timeout=5)

            return stdout, stderr

        except FileNotFoundError:
            raise RuntimeError(
                f"toplev not found at '{self.toplev_path}'. "
                "Install pmu-tools: pip install pmu-tools or clone https://github.com/andikleen/pmu-tools"
            )

    def run_and_parse(self, duration_seconds: int) -> list[ToplevSample]:
        """Run toplev and return parsed samples."""
        stdout, stderr = self.run(duration_seconds)

        if stderr:
            for line in stderr.strip().splitlines():
                if "error" in line.lower() or "warning" in line.lower():
                    logger.warning("toplev: %s", line)

        samples = parse_output(stdout)
        logger.info("Parsed %d samples from toplev output", len(samples))
        return samples


def check_toplev_available(toplev_path: str = "toplev.py") -> bool:
    """Check if toplev is available and runnable."""
    try:
        result = subprocess.run(
            [toplev_path, "--version"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def check_perf_permissions() -> tuple[bool, str]:
    """Check if perf_event_paranoid allows PMU access."""
    try:
        with open("/proc/sys/kernel/perf_event_paranoid", "r") as f:
            level = int(f.read().strip())
        if level <= 1:
            return True, f"perf_event_paranoid={level}"
        return False, (
            f"perf_event_paranoid={level} (too restrictive). "
            "Run: sudo sysctl kernel.perf_event_paranoid=1"
        )
    except (FileNotFoundError, PermissionError, ValueError):
        return False, "Cannot read /proc/sys/kernel/perf_event_paranoid"
