"""Resolve process names to PIDs via /proc."""

import os
import logging

logger = logging.getLogger(__name__)


def resolve_pids(process_name: str, exact: bool = False) -> list[int]:
    """Find PIDs matching process_name, returning only parent processes.

    Checks /proc/*/comm (exact match) and /proc/*/cmdline (substring match).
    When multiple PIDs match, filters to parent-only processes (those whose
    parent PID is NOT in the match set) to avoid passing child/fork PIDs
    to toplev, which causes multiplexing issues on many-core systems.

    Returns sorted list of PIDs.
    """
    pids = set()
    for entry in os.scandir("/proc"):
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        try:
            comm = _read_proc_file(f"/proc/{pid}/comm").strip()
            if exact:
                if comm == process_name:
                    pids.add(pid)
            else:
                if process_name in comm:
                    pids.add(pid)
                    continue
                cmdline = _read_proc_file(f"/proc/{pid}/cmdline")
                if process_name in cmdline:
                    pids.add(pid)
        except (PermissionError, FileNotFoundError, ProcessLookupError):
            continue

    # Filter to parent-only PIDs: exclude children whose ppid is also in the set
    if len(pids) > 1:
        parent_pids = set()
        for pid in pids:
            ppid = _get_ppid(pid)
            if ppid not in pids:
                parent_pids.add(pid)
        if parent_pids:
            logger.info(
                "Filtered %d PIDs to %d parent(s): %s",
                len(pids),
                len(parent_pids),
                sorted(parent_pids),
            )
            pids = parent_pids

    return sorted(pids)


def get_process_comm(pid: int) -> str:
    """Read /proc/{pid}/comm."""
    return _read_proc_file(f"/proc/{pid}/comm").strip()


def get_process_cmdline(pid: int) -> str:
    """Read /proc/{pid}/cmdline as space-joined string."""
    raw = _read_proc_file(f"/proc/{pid}/cmdline")
    return raw.replace("\x00", " ").strip()


def _get_ppid(pid: int) -> int:
    """Read parent PID from /proc/{pid}/status."""
    try:
        status = _read_proc_file(f"/proc/{pid}/status")
        for line in status.splitlines():
            if line.startswith("PPid:"):
                return int(line.split(":")[1].strip())
    except (PermissionError, FileNotFoundError, ProcessLookupError, ValueError):
        pass
    return 0


def _read_proc_file(path: str) -> str:
    with open(path, "r") as f:
        return f.read()
