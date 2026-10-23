"""The M1 GPU report keeps sampled values and their measurement limits together."""

from copy import deepcopy
import importlib.util
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "examples" / "apple_gpu" / "report_instruments.py"
SPEC = importlib.util.spec_from_file_location("report_instruments", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


@pytest.fixture
def profile():
    return {
        "schema": "metal-gpu-counter-intervals", "counter_scope": "device-wide",
        "capture": {"mode": "compute", "probe_source": "packaged", "passes": 2, "items": 65536,
                    "counter_profile": "Performance Limiters", "checksum_verified": True,
                    "target_exit_status": 0, "probe_process_verified": True,
                    "binary_sha256": "a" * 64},
        "probe_coverage": {"status": "sampled", "selected_pid": 42, "passes_observed": 2,
                           "passes_with_counter_samples": 2, "sampled_gpu_work_fraction": 0.25,
                           "sampled_pass_fraction_min": 0.1, "passes_below_10_percent_sampled": 0,
                           "observed_other_activity_fraction_of_sampled_gpu_work": 0.2},
        "sampled_counter_means": {
            "ALU Limiter": {"value": 80, "unit": "%", "sampled_time_ns": 1000},
            "Buffer Write Limiter": {"value": 10, "unit": "%", "sampled_time_ns": 1000},
            "GPU Read Bandwidth": {"value": 2.5, "unit": "GB/s", "sampled_time_ns": 1000},
            "Top Performance Limiter": {"value": 80, "unit": "%", "sampled_time_ns": 1000},
        },
        "sampled_counter_means_without_observed_other_activity": {
            "ALU Limiter": {"value": 90, "unit": "%", "sampled_time_ns": 800},
            "GPU Read Bandwidth": {"value": 1.5, "unit": "GB/s", "sampled_time_ns": 800},
        },
        "sampled_per_pass_metric_distributions": {
            "ALU Limiter": {"sampled_passes": 2, "unit": "%", "minimum": 70, "p10": 72,
                            "median": 80, "p90": 88, "maximum": 90},
        },
        "sampled_per_pass_metric_distributions_without_observed_other_activity": {
            "ALU Limiter": {"sampled_passes": 1, "unit": "%", "minimum": 90, "p10": 90,
                            "median": 90, "p90": 90, "maximum": 90},
        },
        "top_limiter_categories": [
            {"name": "ALU Limiter", "sampled_time_ns": 950,
             "share_of_top_limiter_samples_percent": 95},
            {"name": "Other Limiter", "sampled_time_ns": 50,
             "share_of_top_limiter_samples_percent": 5}],
        "top_limiter_unparsed_sampled_time_ns": 0,
    }


def test_m1_report_preserves_scope_coverage_and_clean_counter_means(profile):
    report = MODULE.report(profile)
    assert report["measurement_kind"] == "m1-instruments-device-wide-sampled-gpu"
    assert report["coverage"]["sampled_gpu_work_fraction"] == 0.25
    assert report["coverage"]["observed_other_activity_fraction_of_sampled_gpu_work"] == 0.2
    assert [row["name"] for row in report["ranked_sampled_percent_metrics"]] == [
        "ALU Limiter", "Buffer Write Limiter"]
    assert report["ranked_sampled_percent_metrics"][0]["mean_without_observed_other_activity_percent"] == 90
    assert report["ranked_sampled_percent_metrics"][0]["clean_sampled_time_fraction"] == 0.8
    assert report["sampled_bandwidth"]["GPU Read Bandwidth"]["sampled_mean_gb_s"] == 2.5
    assert report["capture"]["binary_sha256"] == "a" * 64
    assert report["coverage"]["selected_pid"] == 42
    assert report["sampled_per_pass_metric_distributions"]["ALU Limiter"]["median"] == 80
    assert report["sampled_per_pass_metric_distributions_without_observed_other_activity"][
        "ALU Limiter"]["sampled_passes"] == 1
    text = MODULE.format_report(report)
    assert "device-wide Instruments samples" in text
    assert "Probe process: verified against the compiled binary (PID 42)" in text
    assert "Sampled passes: 2/2; sampled GPU work: 25.00%; least-sampled pass: 10.00%" in text
    assert "Observed other GPU activity within sampled work: 20.00%" in text
    assert "Per-pass ranges use unfiltered, overlap-weighted device-wide samples." in text
    assert "ALU Limiter: 80.00%; clean 90.00% (80.0% of sampled time)" in text
    assert "sampled pass median 80.00%; range 70.00–90.00% (2 passes)" in text
    assert "clean sampled pass median 90.00%; p10–p90 90.00–90.00% (1 pass)" in text


def test_utilization_only_report_has_no_empty_limiter_section(profile):
    profile["sampled_counter_means"].pop("ALU Limiter")
    profile["sampled_counter_means"].pop("Buffer Write Limiter")
    profile["sampled_per_pass_metric_distributions"].pop("ALU Limiter")
    profile["sampled_per_pass_metric_distributions_without_observed_other_activity"].pop("ALU Limiter")
    profile["sampled_counter_means"]["F32 Utilization"] = {
        "value": 60, "unit": "%", "sampled_time_ns": 1000}
    text = MODULE.format_report(MODULE.report(profile))
    assert "Highest sampled limiter means" not in text
    assert "F32 Utilization: 60.00%" in text


def test_report_accepts_profiles_without_per_pass_distributions(profile):
    profile.pop("sampled_per_pass_metric_distributions")
    profile.pop("sampled_per_pass_metric_distributions_without_observed_other_activity")
    profile["capture"].pop("probe_process_verified")
    data = MODULE.report(profile)
    assert data["sampled_per_pass_metric_distributions"] == {}
    assert "Per-pass ranges" not in MODULE.format_report(data)
    assert "Probe process: not independently verified in this profile" in MODULE.format_report(data)


@pytest.mark.parametrize("change,match", [
    ({"counter_scope": "per-encoder"}, "device-wide"),
    ({"capture": {"checksum_verified": False}}, "checksum-verified"),
    ({"capture": {"target_exit_status": False}}, "checksum-verified"),
    ({"capture": {"mode": "unknown"}}, "capture configuration"),
    ({"capture": {"counter_profile": "unknown"}}, "capture configuration"),
    ({"capture": {"items": 16777217}}, "capture configuration"),
    ({"capture": {"probe_process_verified": "yes"}}, "probe process verification"),
    ({"capture": {"binary_sha256": "wrong"}}, "capture hash"),
    ({"probe_coverage": {"selected_pid": None}}, "lacks a selected PID"),
    ({"probe_coverage": {"passes_with_counter_samples": 3}}, "pass coverage"),
    ({"probe_coverage": {"passes_below_10_percent_sampled": 3}}, "sparse-pass"),
    ({"probe_coverage": {"sampled_gpu_work_fraction": float("nan")}}, "sampled GPU work fraction"),
    ({"sampled_counter_means": {"ALU Limiter": {"value": float("inf"), "unit": "%",
                                         "sampled_time_ns": 1000}}},
     "metric value"),
    ({"sampled_counter_means_without_observed_other_activity": {
        "GPU Read Bandwidth": {"value": float("nan"), "unit": "GB/s",
                               "sampled_time_ns": 800}}}, "clean M1 GPU bandwidth"),
    ({"sampled_counter_means_without_observed_other_activity": {
        "ALU Limiter": {"value": 90, "unit": "%", "sampled_time_ns": 1200}}},
     "exceeds total"),
    ({"top_limiter_categories": [{"name": "ALU Limiter", "sampled_time_ns": 950,
                                  "share_of_top_limiter_samples_percent": 105}]}, "top-limiter category"),
    ({"top_limiter_unparsed_sampled_time_ns": 1}, "top-limiter sample time"),
    ({"top_limiter_categories": [
        {"name": "ALU Limiter", "sampled_time_ns": 950, "share_of_top_limiter_samples_percent": 50},
        {"name": "Other Limiter", "sampled_time_ns": 50, "share_of_top_limiter_samples_percent": 50}]},
     "category shares or ordering"),
    ({"top_limiter_categories": [
        {"name": "Other Limiter", "sampled_time_ns": 50, "share_of_top_limiter_samples_percent": 5},
        {"name": "ALU Limiter", "sampled_time_ns": 950, "share_of_top_limiter_samples_percent": 95}]},
     "category shares or ordering"),
    ({"sampled_per_pass_metric_distributions": {
        "ALU Limiter": {"sampled_passes": 3, "unit": "%", "minimum": 70, "p10": 72,
                        "median": 80, "p90": 88, "maximum": 90}}}, "per-pass distribution"),
    ({"sampled_per_pass_metric_distributions": {
        "Unknown Limiter": {"sampled_passes": 1, "unit": None, "minimum": 70, "p10": 72,
                            "median": 80, "p90": 88, "maximum": 90}}}, "per-pass distribution"),
    ({"sampled_per_pass_metric_distributions_without_observed_other_activity": {
        "ALU Limiter": {"sampled_passes": 3, "unit": "%", "minimum": 90, "p10": 90,
                        "median": 90, "p90": 90, "maximum": 90}}}, "per-pass distribution"),
])
def test_m1_report_rejects_invalid_provenance_or_sampled_values(profile, change, match):
    altered = deepcopy(profile)
    for section, updates in change.items():
        if isinstance(updates, dict):
            altered[section].update(updates)
        else:
            altered[section] = updates
    with pytest.raises(ValueError, match=match):
        MODULE.report(altered)
