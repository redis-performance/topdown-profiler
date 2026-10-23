"""Xcode guided CPU Counters are native Apple ratios, distinct from Intel slot TMA."""

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from topdown.collector.apple import AppleProfileError, export_profile, parse_apple_bottlenecks
from topdown.collector.apple_probe import probe


def metric_xml():
    columns = {"timestamp": "start-time", "duration": "duration", "pmi-event": "string",
               "metric-display-name": "string", "thread": "thread", "process": "process",
               "metric-value": "fixed-decimal", "core": "core", "is-ratio": "boolean",
               "documentation": "markdown-text"}
    header = "".join(f"<col><mnemonic>{name}</mnemonic><engineering-type>{kind}</engineering-type></col>"
                     for name, kind in columns.items())
    rows = []
    for time, pid, core, cycles, shares in (
        (100, 42, 7, 100, (0.1, 0.2, 0.3, 0.4)),
        (200, 42, 1, 300, (0.5, 0.2, 0.1, 0.2)),
        (300, 99, 7, 400, (0.25, 0.25, 0.25, 0.25)),
    ):
        for event, value in zip(("cycle", "delivery", "discarded", "processing", "useful"),
                                (cycles, *shares)):
            rows.append(f'<row><start-time>{time}</start-time><duration>100</duration>'
                        f'<string>{event}</string><string>{event}</string>'
                        f'<thread><tid>2</tid><process><pid>{pid}</pid></process></thread>'
                        f'<process><pid>{pid}</pid></process><fixed-decimal>{value}</fixed-decimal>'
                        f'<core fmt="CPU {core}">{core}</core>'
                        f'<boolean>{int(event != "cycle")}</boolean><sentinel/></row>')
    return f'<trace-query-result><node><schema name="MetricTable">{header}</schema>{"".join(rows)}</node></trace-query-result>'


def test_guided_ratios_are_cycle_weighted_and_scoped(tmp_path):
    path = tmp_path / "metrics.xml"
    path.write_text(metric_xml())
    profile = parse_apple_bottlenecks(path, 42)
    assert profile["sample_count"] == 2
    assert profile["excluded_rows"] == 5
    assert profile["valid_cycle_fraction_of_scoped_intervals"] == 1
    assert profile["apple_bottlenecks"]["cycles"] == 400
    assert profile["apple_bottlenecks"]["weighting"] == "sampled_core_cycles"
    assert profile["apple_bottlenecks_available"] is True
    assert profile["apple_bottlenecks"]["shares"] == pytest.approx(
        {"delivery": 0.4, "discarded": 0.2, "processing": 0.15, "useful": 0.25})
    assert [core["core"] for core in profile["apple_bottlenecks_by_core"]] == [1, 7]
    assert profile["tma_available"] is False
    assert set(profile["tma"].values()) == {None}
    window = parse_apple_bottlenecks(path, 42, end_ns=200)
    assert window["sample_count"] == 1
    assert window["apple_bottlenecks"]["shares"]["delivery"] == pytest.approx(0.1)


def test_bad_guided_metric_is_not_reported_as_complete(tmp_path):
    path = tmp_path / "metrics.xml"
    path.write_text(metric_xml().replace("<fixed-decimal>0.4</fixed-decimal>",
                                         "<fixed-decimal>0.5</fixed-decimal>"))
    profile = parse_apple_bottlenecks(path, 42)
    assert profile["sample_count"] == 1
    assert profile["invalid_intervals"] == 1
    assert profile["invalid_cycles_with_known_count"] == 100
    assert profile["valid_cycle_fraction_of_scoped_intervals"] == 0.75
    with pytest.raises(AppleProfileError, match="No valid guided"):
        parse_apple_bottlenecks(path, 42, end_ns=200)


def test_missing_cycle_count_does_not_claim_known_guided_coverage(tmp_path):
    path = tmp_path / "metrics.xml"
    xml = metric_xml()
    start = xml.index("<row>")
    end = xml.index("</row>", start) + len("</row>")
    path.write_text(xml[:start] + xml[end:])
    profile = parse_apple_bottlenecks(path, 42)
    assert profile["sample_count"] == 1
    assert profile["invalid_intervals"] == 1
    assert profile["invalid_intervals_without_cycle_count"] == 1
    assert profile["valid_cycle_fraction_of_scoped_intervals"] is None


def test_guided_cycle_count_keeps_full_uint64_precision(tmp_path):
    path = tmp_path / "metrics.xml"
    path.write_text(metric_xml().replace("<fixed-decimal>100</fixed-decimal>",
                                         "<fixed-decimal>9007199254740993</fixed-decimal>", 1))
    profile = parse_apple_bottlenecks(path, 42)
    assert profile["apple_bottlenecks"]["cycles"] == 9007199254741293


@pytest.mark.parametrize("old,new,message", [
    ("<boolean>0</boolean>", "<boolean>1</boolean>", "Invalid guided"),
    ("<fixed-decimal>0.4</fixed-decimal>", "<fixed-decimal>1.4</fixed-decimal>", "Invalid guided"),
    ("<string>discarded</string>", "<string>unexpected</string>", "Unexpected guided"),
])
def test_guided_schema_fails_closed_on_wrong_metric(tmp_path, old, new, message):
    path = tmp_path / "metrics.xml"
    path.write_text(metric_xml().replace(old, new, 1))
    with pytest.raises(AppleProfileError, match=message):
        parse_apple_bottlenecks(path, 42)


def test_xcode_guided_schema_is_selected_only_for_counters(tmp_path):
    def fake(cmd, **kwargs):
        path = Path(cmd[cmd.index("--output") + 1])
        if "--toc" in cmd:
            path.write_text('<trace-toc><run number="1"><info><target><process pid="42" '
                            'return-exit-status="0"/></target></info><data><table schema="MetricTable"/>'
                            '</data></run></trace-toc>')
        else:
            path.write_text(metric_xml())
        return subprocess.CompletedProcess(cmd, 0, "", "")

    with patch("subprocess.run", side_effect=fake):
        profile = export_profile(tmp_path / "trace", tmp_path / "out", "counters")
    assert profile["schema"] == "MetricTable"
    assert profile["measurement_kind"] == "apple-guided-bottleneck-intervals"
    assert profile["target_exit_status"] == "0"


def test_guided_probe_reports_native_shares(tmp_path):
    profile = {"schema": "MetricTable", "sample_count": 2,
               "measurement_kind": "apple-guided-bottleneck-intervals",
               "apple_bottlenecks": {"cycles": 400, "shares": dict.fromkeys(
                   ("delivery", "discarded", "processing", "useful"), 0.25)}}
    with patch("topdown.collector.apple_probe.doctor", return_value={"xctrace": "installed",
                                                                 "templates": ["CPU Counters"]}), \
         patch("topdown.collector.apple_probe._run"), \
         patch("topdown.collector.apple_probe.record_profile", return_value=profile), \
         patch("topdown.collector.apple_probe._target_completed", return_value=True):
        result = probe(tmp_path / "probe", "counters")
    assert result["status"] == "counters_present"
    assert result["apple_bottlenecks_available"] is True
    assert result["apple_bottlenecks"] == profile["apple_bottlenecks"]
    assert result["tma_available"] is False
