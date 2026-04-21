"""Tests for the vendor-aware knowledge dispatcher."""

from unittest.mock import patch, mock_open

import pytest

from topdown.knowledge import (
    _reset_vendor_cache,
    active_vendor,
    get_children,
    get_metric_info,
    list_all_metrics,
)


@pytest.fixture(autouse=True)
def _clear_cache():
    _reset_vendor_cache()
    yield
    _reset_vendor_cache()


# ── vendor detection ────────────────────────────────────────────────


class TestActiveVendor:
    def test_detects_amd(self):
        cpuinfo = "vendor_id\t: AuthenticAMD\n"
        with patch("builtins.open", mock_open(read_data=cpuinfo)):
            assert active_vendor() == "amd"

    def test_detects_intel(self):
        cpuinfo = "vendor_id\t: GenuineIntel\n"
        with patch("builtins.open", mock_open(read_data=cpuinfo)):
            assert active_vendor() == "intel"

    def test_missing_cpuinfo_defaults_to_intel(self):
        with patch("builtins.open", side_effect=OSError("no cpuinfo")):
            assert active_vendor() == "intel"

    def test_caches_result(self):
        cpuinfo = "vendor_id\t: AuthenticAMD\n"
        with patch("builtins.open", mock_open(read_data=cpuinfo)) as m:
            assert active_vendor() == "amd"
            # second call should hit cache
            assert active_vendor() == "amd"
            assert m.call_count == 1


# ── routing ─────────────────────────────────────────────────────────


class TestRouting:
    def test_amd_routes_to_amd_kb(self):
        info = get_metric_info("Backend_Bound.Memory_Bound", vendor="amd")
        assert info is not None
        assert isinstance(info, dict)
        desc = info["description"].lower()
        assert "ccd" in desc or "infinityfabric" in desc

    def test_intel_routes_to_intel_kb(self):
        info = get_metric_info("Backend_Bound.Memory_Bound", vendor="intel")
        assert info is not None
        assert isinstance(info, dict)
        # Intel KB has specific Intel remediation; AMD text should not leak
        desc = info["description"].lower()
        assert "authenticamd" not in desc

    def test_amd_list_differs_from_intel(self):
        amd = set(list_all_metrics(vendor="amd"))
        intel = set(list_all_metrics(vendor="intel"))
        # Intel KB is much larger (L4-L6 metrics); AMD is focused L1+L2
        assert len(intel) > len(amd)
        # L1 overlap
        assert {"Frontend_Bound", "Backend_Bound", "Bad_Speculation", "Retiring"} <= amd
        assert {"Frontend_Bound", "Backend_Bound", "Bad_Speculation", "Retiring"} <= intel

    def test_amd_children_has_fetch_latency(self):
        children = get_children("Frontend_Bound", vendor="amd")
        assert "Frontend_Bound.Fetch_Latency" in children
        assert "Frontend_Bound.Fetch_Bandwidth" in children

    def test_unknown_metric_returns_none_amd(self):
        assert get_metric_info("NoSuch.Metric", vendor="amd") is None

    def test_unknown_metric_returns_none_intel(self):
        assert get_metric_info("NoSuch.Metric", vendor="intel") is None


# ── auto-detect path ────────────────────────────────────────────────


class TestAutoDetectPath:
    def test_amd_host_autodetects(self):
        cpuinfo = "vendor_id\t: AuthenticAMD\n"
        with patch("builtins.open", mock_open(read_data=cpuinfo)):
            info = get_metric_info("Backend_Bound.Memory_Bound")
            assert info is not None
            assert "ccd" in info["description"].lower() or "infinityfabric" in info["description"].lower()

    def test_intel_host_autodetects(self):
        cpuinfo = "vendor_id\t: GenuineIntel\n"
        with patch("builtins.open", mock_open(read_data=cpuinfo)):
            info = get_metric_info("Frontend_Bound")
            assert info is not None
            # Intel descriptions mention uop cache or DSB
            assert "frontend" in info["description"].lower()

    def test_vendor_override_wins(self):
        """Even on an Intel host, vendor='amd' should give AMD advice."""
        cpuinfo = "vendor_id\t: GenuineIntel\n"
        with patch("builtins.open", mock_open(read_data=cpuinfo)):
            info = get_metric_info("Backend_Bound.Memory_Bound", vendor="amd")
            assert info is not None
            assert "ccd" in info["description"].lower() or "infinityfabric" in info["description"].lower()
