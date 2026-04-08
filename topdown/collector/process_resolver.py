"""Resolve process names to PIDs via /proc."""

import os
import logging

logger = logging.getLogger(__name__)


def resolve_pids(process_name: str, exact: bool = False) -> list[int]:
    """Find all PIDs matching process_name.

    Checks /proc/*/comm (exact match) and /proc/*/cmdline (substring match).
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
    return sorted(pids)


def get_process_comm(pid: int) -> str:
    """Read /proc/{pid}/comm."""
    return _read_proc_file(f"/proc/{pid}/comm").strip()


def get_process_cmdline(pid: int) -> str:
    """Read /proc/{pid}/cmdline as space-joined string."""
    raw = _read_proc_file(f"/proc/{pid}/cmdline")
    return raw.replace("\x00", " ").strip()


def _read_proc_file(path: str) -> str:
    with open(path, "r") as f:
        return f.read()
