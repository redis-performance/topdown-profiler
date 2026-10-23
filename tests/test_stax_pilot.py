"""Pilot budgets and Linux resource accounting must remain explicit."""

import importlib.util
from pathlib import Path


def module():
    path = Path(__file__).resolve().parents[1] / "examples/stax/redis_pilot.py"
    spec = importlib.util.spec_from_file_location("stax_pilot_test", path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def test_pilot_budget_and_window_validity():
    pilot = module()
    budget = {"median_throughput_loss_pct": 10, "median_ping_p99_increase_pct": 20, "max_baseline_rate_range_pct": 10}
    baseline = {
        "pair": 0,
        "mode": "baseline",
        "commands_per_second": 100,
        "ping_rtt_ms": {"p99": 1},
        "probe_errors": [],
    }
    profile = {
        **baseline,
        "mode": "profile",
        "commands_per_second": 95,
        "ping_rtt_ms": {"p99": 1.1},
        "measurement_inside_capture": True,
        "measurement_inside_observed_clock": True,
    }
    assert pilot.summarize([], budget)["status"] == "insufficient_pairs"
    assert pilot.summarize([baseline, profile], budget)["status"] == "within_pilot_budget"
    assert (
        pilot.summarize([baseline, {**profile, "commands_per_second": 50}], budget)["status"] == "exceeds_pilot_budget"
    )
    assert (
        pilot.summarize([baseline, {**profile, "measurement_inside_observed_clock": False}], budget)["status"]
        == "inconclusive"
    )
    assert pilot.percentile([], 0.99) is None
    assert pilot.percentile([3, 1, 2], 0.99) == 3


def test_linux_proc_identity_and_cpu_fields(monkeypatch):
    pilot = module()
    fields = ["S"] + ["0"] * 21
    fields[11], fields[12], fields[19], fields[21] = "100", "50", "123456", "7"
    monkeypatch.setattr(Path, "read_text", lambda *a, **k: "42 (command ) with spaces) " + " ".join(fields))
    result = pilot.process_snapshot(42)
    assert result["start_ticks"] == 123456 and result["cpu_ticks"] == 150
    assert result["rss_bytes"] > 0


def test_missing_process_resources_are_unavailable(monkeypatch):
    pilot = module()

    def missing(*args, **kwargs):
        raise FileNotFoundError("exited")

    monkeypatch.setattr(Path, "read_text", missing)
    assert pilot.process_snapshot(42) is None
