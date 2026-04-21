"""Knowledge-base dispatcher — routes metric lookups to Intel or AMD KB.

Public API mirrors ``metrics.py`` but auto-selects between the Intel and
AMD knowledge bases based on CPU vendor (``AuthenticAMD`` in
``/proc/cpuinfo``). Callers can also pass an explicit vendor override.

Examples::

    from topdown.knowledge import get_metric_info, list_all_metrics

    # Auto-detect vendor
    info = get_metric_info("Backend_Bound.Memory_Bound")

    # Force AMD advice
    info = get_metric_info("Backend_Bound.Memory_Bound", vendor="amd")
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional


_VENDOR_CACHE: str | None = None


def _detect_vendor() -> str:
    """Return ``"amd"`` or ``"intel"`` based on ``/proc/cpuinfo``. Cached."""
    global _VENDOR_CACHE
    if _VENDOR_CACHE is not None:
        return _VENDOR_CACHE
    try:
        with open("/proc/cpuinfo", "r") as f:
            for line in f:
                if line.lower().startswith("vendor_id"):
                    _, _, value = line.partition(":")
                    vid = value.strip()
                    _VENDOR_CACHE = "amd" if vid == "AuthenticAMD" else "intel"
                    return _VENDOR_CACHE
    except OSError:
        pass
    _VENDOR_CACHE = "intel"  # safe default
    return _VENDOR_CACHE


def _reset_vendor_cache() -> None:
    """Clear cached vendor detection. Intended for tests."""
    global _VENDOR_CACHE
    _VENDOR_CACHE = None


def _resolve_vendor(vendor: str | None) -> str:
    """Normalize a vendor string to ``"amd"`` or ``"intel"``."""
    if vendor is None:
        return _detect_vendor()
    v = vendor.strip().lower()
    if v in ("amd", "authenticamd", "uprof_pcm"):
        return "amd"
    return "intel"


def get_metric_info(metric_name: str, vendor: str | None = None) -> Optional[Dict[str, Any]]:
    """Look up a TMA metric by exact or partial (leaf) name.

    Auto-selects the Intel or AMD knowledge base via ``/proc/cpuinfo``
    vendor_id, unless ``vendor`` is passed explicitly.

    On AMD hosts the AMD KB is tried first; if the metric isn't present
    (e.g. deep Intel-specific L3/L4 nodes like ``DRAM_Bound``), we fall
    through to the Intel KB — its descriptions are architecture-neutral
    enough to still help when AMD-specific advice isn't available.

    Returns the same shape as the underlying KB: either a single metric
    dict, a dict of ``{full_path: metric_dict}`` if the leaf name is
    ambiguous, or ``None`` if nothing matches either KB.
    """
    v = _resolve_vendor(vendor)
    if v == "amd":
        from topdown.knowledge import metrics_amd
        info = metrics_amd.get_metric_info(metric_name)
        if info is not None:
            return info
    from topdown.knowledge import metrics
    return metrics.get_metric_info(metric_name)


def list_all_metrics(vendor: str | None = None) -> List[str]:
    """Return a sorted list of known metric names for the active vendor.

    On AMD, returns the AMD-specific names. On Intel, returns the full
    Intel KB.  The dispatcher does NOT merge — list scopes are intentionally
    per-vendor so callers can surface vendor-appropriate metric lists
    (the merged union would mislead users about what is AMD-native).
    """
    v = _resolve_vendor(vendor)
    if v == "amd":
        from topdown.knowledge import metrics_amd
        return metrics_amd.list_all_metrics()
    from topdown.knowledge import metrics
    return metrics.list_all_metrics()


def get_children(metric_name: str, vendor: str | None = None) -> List[str]:
    """Return direct children of a TMA node for the active vendor.

    On AMD: if the node has children in the AMD KB, return those; else
    fall back to the Intel KB (so queries for deeper Intel nodes still
    produce useful hierarchy on AMD hosts).
    """
    v = _resolve_vendor(vendor)
    if v == "amd":
        from topdown.knowledge import metrics_amd
        children = metrics_amd.get_children(metric_name)
        if children:
            return children
    from topdown.knowledge import metrics
    return metrics.get_children(metric_name)


def active_vendor() -> str:
    """Return the active vendor (``"amd"`` or ``"intel"``)."""
    return _detect_vendor()
