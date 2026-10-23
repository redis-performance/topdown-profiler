import importlib.util
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "examples" / "apple_gpu" / "summarize_counters.py"
SPEC = importlib.util.spec_from_file_location("summarize_counters", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_summary_resolves_xctrace_references():
    source = b"""<trace-query-result><node><schema name="metal-gpu-counter-intervals"/>
    <row><start-time id="1">100</start-time><duration>20</duration><uint32>0</uint32>
    <gpu-counter-name id="2">ALU Limiter</gpu-counter-name></row>
    <row><start-time ref="1"/><duration>20</duration><uint32>1</uint32>
    <gpu-counter-name ref="2"/></row>
    </node></trace-query-result>"""
    assert MODULE.summarize(source) == {
        "schema": "metal-gpu-counter-intervals",
        "counter_scope": "device-wide",
        "counter_rows": 2,
        "sample_intervals": 1,
        "counter_names": {"ALU Limiter": 2},
    }


def test_summary_rejects_wrong_schema():
    with pytest.raises(ValueError, match="expected"):
        MODULE.summarize(b'<trace-query-result><schema name="gpu-intervals"/></trace-query-result>')


def test_summary_uses_counter_column_names_when_export_order_changes():
    source = b"""<trace-query-result><node><schema name="metal-gpu-counter-intervals">
    <col><mnemonic>name</mnemonic></col><col><mnemonic>duration</mnemonic></col>
    <col><mnemonic>counter-id</mnemonic></col><col><mnemonic>start</mnemonic></col>
    </schema><row><gpu-counter-name>ALU Limiter</gpu-counter-name><duration>20</duration>
    <uint32>0</uint32><start-time>100</start-time></row></node></trace-query-result>"""
    result = MODULE.summarize(source)
    assert result["sample_intervals"] == 1
    assert result["counter_names"] == {"ALU Limiter": 1}


def test_summary_distinguishes_counter_intervals_with_the_same_start():
    source = b"""<trace-query-result><node><schema name="metal-gpu-counter-intervals"/>
    <row><start-time>100</start-time><duration>10</duration><uint32>0</uint32>
    <gpu-counter-name>ALU Limiter</gpu-counter-name></row>
    <row><start-time>100</start-time><duration>20</duration><uint32>0</uint32>
    <gpu-counter-name>GPU Read Bandwidth</gpu-counter-name></row>
    </node></trace-query-result>"""
    result = MODULE.summarize(source)
    assert result["counter_rows"] == 2
    assert result["sample_intervals"] == 2


@pytest.mark.parametrize("second_start", [90, 100, 105])
def test_summary_rejects_overlapping_or_out_of_order_metric_samples(second_start):
    source = f'''<trace-query-result><node><schema name="metal-gpu-counter-intervals">
    <col><mnemonic>start</mnemonic></col><col><mnemonic>duration</mnemonic></col>
    <col><mnemonic>name</mnemonic></col><col><mnemonic>label</mnemonic></col>
    <col><mnemonic>value</mnemonic></col><col><mnemonic>is-percentage</mnemonic></col>
    </schema><row><start-time>100</start-time><duration>10</duration>
    <gpu-counter-name fmt="ALU Limiter">1.ALU Limiter</gpu-counter-name><formatted-label fmt="10%"/>
    <fixed-decimal>10</fixed-decimal><boolean>1</boolean></row>
    <row><start-time>{second_start}</start-time><duration>10</duration>
    <gpu-counter-name fmt="ALU Limiter">1.ALU Limiter</gpu-counter-name><formatted-label fmt="20%"/>
    <fixed-decimal>20</fixed-decimal><boolean>1</boolean></row>
    </node></trace-query-result>'''.encode()
    with pytest.raises(ValueError, match="overlapping or out-of-order"):
        MODULE.summarize(source)


def test_summary_checks_probe_counter_overlap():
    counters = b"""<trace-query-result><node><schema name="metal-gpu-counter-intervals"/>
    <row><start-time id="1">105</start-time><duration id="2">10</duration><uint32>0</uint32>
    <gpu-counter-name id="3">ALU Limiter</gpu-counter-name></row>
    <row><start-time>145</start-time><duration ref="2"/><uint32>0</uint32>
    <gpu-counter-name ref="3"/></row></node></trace-query-result>"""
    gpu = b"""<trace-query-result><node><schema name="metal-gpu-intervals">
    <col><mnemonic>start</mnemonic></col><col><mnemonic>duration</mnemonic></col>
    <col><mnemonic>event-label</mnemonic></col></schema>
    <row><start-time>100</start-time><duration>20</duration>
    <formatted-label fmt="topdown GPU probe pass 0:burn compute pass 0 (probe)"/></row>
    <row><start-time>200</start-time><duration>20</duration>
    <formatted-label fmt="topdown GPU probe pass 1:burn compute pass 1 (probe)"/></row>
    </node></trace-query-result>"""
    result = MODULE.summarize(counters, gpu)["probe_coverage"]
    assert result == {
        "selected_pid": None,
        "status": "sampled",
        "workloads_observed": ["burn"],
        "passes_observed": 2,
        "pass_index_min": 0,
        "pass_index_max": 1,
        "passes_with_counter_samples": 1,
        "passes_with_counter_samples_by_metric": {},
        "sampled_pass_fraction_min": 0,
        "sampled_pass_fraction_median": 0.25,
        "sampled_pass_fraction_max": 0.5,
        "passes_below_10_percent_sampled": 1,
        "gpu_work_time_ns": 40,
        "sampled_gpu_work_time_ns": 10,
        "sampled_gpu_work_fraction": 0.25,
        "counter_sample_intervals_overlapping": 1,
        "other_active_gpu_time_overlapping_ns": None,
        "other_active_gpu_time_in_counter_samples_ns": None,
        "observed_other_activity_fraction_of_sampled_gpu_work": None,
        "last_counter_end_minus_last_probe_end_ns": -65,
    }


@pytest.mark.parametrize("mode", ["compute", "alu", "memory", "stream"])
def test_summary_recognizes_packaged_probe_labels(mode):
    counters = b"""<trace-query-result><node><schema name="metal-gpu-counter-intervals"/>
    <row><start-time>100</start-time><duration>10</duration><uint32>0</uint32>
    <gpu-counter-name>ALU Limiter</gpu-counter-name></row></node></trace-query-result>"""
    gpu = (f'<trace-query-result><node><schema name="metal-gpu-intervals">'
           '<col><mnemonic>start</mnemonic></col><col><mnemonic>duration</mnemonic></col>'
           '<col><mnemonic>event-label</mnemonic></col></schema>'
           f'<row><start-time>100</start-time><duration>10</duration>'
           f'<formatted-label fmt="topdown GPU {mode} pass 0:{mode} compute pass 0 (probe)"/></row>'
           f'<row><start-time>200</start-time><duration>10</duration>'
           f'<formatted-label fmt="topdown GPU {mode} pass 1:{mode} compute pass 2 (probe)"/></row>'
           '</node></trace-query-result>').encode()
    coverage = MODULE.summarize(counters, gpu)["probe_coverage"]
    assert coverage["workloads_observed"] == [mode]
    assert coverage["passes_observed"] == 1
    assert coverage["passes_with_counter_samples"] == 1


def test_summary_reports_counter_rows_without_probe_coverage():
    counters = b"""<trace-query-result><node><schema name="metal-gpu-counter-intervals"/>
    <row><start-time>10</start-time><duration>2</duration><uint32>0</uint32>
    <gpu-counter-name>ALU Limiter</gpu-counter-name></row></node></trace-query-result>"""
    gpu = b"""<trace-query-result><node><schema name="metal-gpu-intervals">
    <col><mnemonic>start</mnemonic></col><col><mnemonic>duration</mnemonic></col>
    <col><mnemonic>event-label</mnemonic></col></schema>
    <row><start-time>100</start-time><duration>20</duration>
    <formatted-label fmt="topdown GPU probe pass 0:burn compute pass 0"/></row>
    </node></trace-query-result>"""
    result = MODULE.summarize(counters, gpu)["probe_coverage"]
    assert result["passes_observed"] == 1
    assert result["workloads_observed"] == ["burn"]
    assert result["passes_with_counter_samples"] == 0
    assert result["sampled_pass_fraction_min"] == 0
    assert result["passes_below_10_percent_sampled"] == 1
    assert result["status"] == "no_counter_overlap"
    assert result["sampled_gpu_work_time_ns"] == 0
    assert result["sampled_gpu_work_fraction"] == 0
    assert result["observed_other_activity_fraction_of_sampled_gpu_work"] is None


def test_one_nanosecond_overlap_does_not_hide_sparse_pass_sampling():
    counters = b"""<trace-query-result><node><schema name="metal-gpu-counter-intervals"/>
    <row><start-time>199</start-time><duration>2</duration><uint32>0</uint32>
    <gpu-counter-name>ALU Limiter</gpu-counter-name></row></node></trace-query-result>"""
    gpu = b"""<trace-query-result><node><schema name="metal-gpu-intervals">
    <col><mnemonic>start</mnemonic></col><col><mnemonic>duration</mnemonic></col>
    <col><mnemonic>event-label</mnemonic></col></schema>
    <row><start-time>100</start-time><duration>100</duration>
    <formatted-label fmt="topdown GPU probe pass 0:burn compute pass 0"/></row>
    </node></trace-query-result>"""
    coverage = MODULE.summarize(counters, gpu)["probe_coverage"]
    assert coverage["passes_with_counter_samples"] == 1
    assert coverage["sampled_pass_fraction_min"] == 0.01
    assert coverage["sampled_pass_fraction_median"] == 0.01
    assert coverage["sampled_pass_fraction_max"] == 0.01
    assert coverage["passes_below_10_percent_sampled"] == 1


def test_summary_exposes_missing_metric_on_a_sampled_pass():
    counters = b"""<trace-query-result><node><schema name="metal-gpu-counter-intervals">
    <col><mnemonic>start</mnemonic></col><col><mnemonic>duration</mnemonic></col>
    <col><mnemonic>name</mnemonic></col><col><mnemonic>label</mnemonic></col>
    <col><mnemonic>value</mnemonic></col><col><mnemonic>is-percentage</mnemonic></col></schema>
    <row><start-time>100</start-time><duration>10</duration>
    <gpu-counter-name fmt="ALU Limiter">1.ALU Limiter</gpu-counter-name><formatted-label fmt="10%"/>
    <fixed-decimal>10</fixed-decimal><boolean>1</boolean></row>
    <row><start-time>200</start-time><duration>10</duration>
    <gpu-counter-name fmt="Buffer Write Limiter">2.Buffer Write Limiter</gpu-counter-name><formatted-label fmt="20%"/>
    <fixed-decimal>20</fixed-decimal><boolean>1</boolean></row>
    </node></trace-query-result>"""
    gpu = b"""<trace-query-result><node><schema name="metal-gpu-intervals">
    <col><mnemonic>start</mnemonic></col><col><mnemonic>duration</mnemonic></col>
    <col><mnemonic>event-label</mnemonic></col></schema>
    <row><start-time>100</start-time><duration>10</duration>
    <formatted-label fmt="topdown GPU probe pass 0:burn compute pass 0"/></row>
    <row><start-time>200</start-time><duration>10</duration>
    <formatted-label fmt="topdown GPU probe pass 1:burn compute pass 1"/></row>
    </node></trace-query-result>"""
    coverage = MODULE.summarize(counters, gpu)["probe_coverage"]
    assert coverage["passes_with_counter_samples"] == 2
    assert coverage["passes_with_counter_samples_by_metric"] == {
        "ALU Limiter": 1, "Buffer Write Limiter": 1,
    }


def test_summary_distributes_sampled_metric_means_by_labeled_pass():
    counters = b"""<trace-query-result><node><schema name="metal-gpu-counter-intervals">
    <col><mnemonic>start</mnemonic></col><col><mnemonic>duration</mnemonic></col>
    <col><mnemonic>name</mnemonic></col><col><mnemonic>label</mnemonic></col>
    <col><mnemonic>value</mnemonic></col><col><mnemonic>is-percentage</mnemonic></col></schema>
    <row><start-time>100</start-time><duration>10</duration>
    <gpu-counter-name fmt="ALU Limiter">1.ALU Limiter</gpu-counter-name><formatted-label fmt="10%"/>
    <fixed-decimal>10</fixed-decimal><boolean>1</boolean></row>
    <row><start-time>200</start-time><duration>10</duration>
    <gpu-counter-name fmt="ALU Limiter">1.ALU Limiter</gpu-counter-name><formatted-label fmt="30%"/>
    <fixed-decimal>30</fixed-decimal><boolean>1</boolean></row>
    <row><start-time>300</start-time><duration>10</duration>
    <gpu-counter-name fmt="GPU Read Bandwidth">2.GPU Read Bandwidth</gpu-counter-name>
    <formatted-label fmt="2 GB/s"/><fixed-decimal>2</fixed-decimal><boolean>0</boolean></row>
    </node></trace-query-result>"""
    gpu = b"""<trace-query-result><node><schema name="metal-gpu-intervals">
    <col><mnemonic>start</mnemonic></col><col><mnemonic>duration</mnemonic></col>
    <col><mnemonic>event-label</mnemonic></col></schema>
    <row><start-time>100</start-time><duration>10</duration>
    <formatted-label fmt="topdown GPU probe pass 0:burn compute pass 0"/></row>
    <row><start-time>200</start-time><duration>10</duration>
    <formatted-label fmt="topdown GPU probe pass 1:burn compute pass 1"/></row>
    <row><start-time>300</start-time><duration>10</duration>
    <formatted-label fmt="topdown GPU probe pass 2:burn compute pass 2"/></row>
    </node></trace-query-result>"""
    result = MODULE.summarize(counters, gpu)
    assert result["sampled_per_pass_metric_distributions"]["ALU Limiter"] == {
        "sampled_passes": 2, "unit": "%", "minimum": 10, "p10": 12,
        "median": 20, "p90": 28, "maximum": 30,
    }
    assert result["sampled_per_pass_metric_distributions"]["GPU Read Bandwidth"]["sampled_passes"] == 1
    assert result["sampled_per_pass_metric_distributions_without_observed_other_activity"] is None
    assert result["probe_coverage"]["passes_with_counter_samples"] == 3


def test_idle_counter_rows_do_not_claim_probe_work():
    counters = b"""<trace-query-result><node><schema name="metal-gpu-counter-intervals"/>
    <row><start-time>100</start-time><duration>20</duration><uint32>0</uint32>
    <gpu-counter-name>ALU Limiter</gpu-counter-name></row></node></trace-query-result>"""
    gpu = b"""<trace-query-result><node><schema name="metal-gpu-intervals">
    <col><mnemonic>start</mnemonic></col><col><mnemonic>duration</mnemonic></col>
    <col><mnemonic>event-label</mnemonic></col><col><mnemonic>state</mnemonic></col></schema>
    <row><start-time>100</start-time><duration>20</duration>
    <formatted-label fmt="other process"/><gpu-state fmt="Active"/></row>
    </node></trace-query-result>"""
    result = MODULE.summarize(counters, gpu)
    assert result["counter_rows"] == 1
    assert result["probe_coverage"]["status"] == "no_probe_intervals"
    assert result["probe_coverage"]["passes_observed"] == 0
    assert result["probe_coverage"]["sampled_pass_fraction_min"] is None
    assert result["sampled_counter_means"] == {}


def test_summary_counts_other_active_gpu_overlap():
    counters = b'<trace-query-result><schema name="metal-gpu-counter-intervals"/></trace-query-result>'
    gpu = b"""<trace-query-result><node><schema name="metal-gpu-intervals">
    <col><mnemonic>start</mnemonic></col><col><mnemonic>duration</mnemonic></col>
    <col><mnemonic>event-label</mnemonic></col><col><mnemonic>state</mnemonic></col></schema>
    <row><start-time>100</start-time><duration>20</duration>
    <formatted-label fmt="topdown GPU probe pass 0:stream compute pass 0"/>
    <gpu-state fmt="Active"/></row>
    <row><start-time>110</start-time><duration>20</duration>
    <formatted-label fmt="other GPU activity"/><gpu-state fmt="Active"/></row>
    <row><start-time>110</start-time><duration>20</duration>
    <formatted-label fmt="inactive"/><gpu-state fmt="Inactive"/></row>
    </node></trace-query-result>"""
    result = MODULE.summarize(counters, gpu)["probe_coverage"]
    assert result["workloads_observed"] == ["stream"]
    assert result["other_active_gpu_time_overlapping_ns"] == 10
    assert result["other_active_gpu_time_in_counter_samples_ns"] == 0


def test_summary_weights_sampled_values_and_keeps_units():
    counters = b"""<trace-query-result><node><schema name="metal-gpu-counter-intervals">
    <col><mnemonic>start</mnemonic></col><col><mnemonic>duration</mnemonic></col>
    <col><mnemonic>counter-id</mnemonic></col><col><mnemonic>name</mnemonic></col>
    <col><mnemonic>label</mnemonic></col><col><mnemonic>value</mnemonic></col>
    <col><mnemonic>is-percentage</mnemonic></col></schema>
    <row><start-time>100</start-time><duration>10</duration><uint32>0</uint32>
    <gpu-counter-name fmt="ALU Limiter">1.ALU Limiter</gpu-counter-name>
    <formatted-label fmt="10%"/><fixed-decimal>10</fixed-decimal><boolean>1</boolean></row>
    <row><start-time>110</start-time><duration>20</duration><uint32>0</uint32>
    <gpu-counter-name fmt="ALU Limiter">1.ALU Limiter</gpu-counter-name>
    <formatted-label fmt="30%"/><fixed-decimal>30</fixed-decimal><boolean>1</boolean></row>
    <row><start-time>100</start-time><duration>10</duration><uint32>1</uint32>
    <gpu-counter-name fmt="GPU Read Bandwidth">2.GPU Read Bandwidth</gpu-counter-name>
    <formatted-label fmt="2 GB/s"/><fixed-decimal>2</fixed-decimal><boolean>0</boolean></row>
    <row><start-time>100</start-time><duration>30</duration><uint32>2</uint32>
    <gpu-counter-name fmt="Top Performance Limiter">-2.Top Performance Limiter</gpu-counter-name>
    <formatted-label fmt="30% (1.ALU Limiter)"/><fixed-decimal>30</fixed-decimal><boolean>1</boolean></row>
    </node></trace-query-result>"""
    gpu = b"""<trace-query-result><node><schema name="metal-gpu-intervals">
    <col><mnemonic>start</mnemonic></col><col><mnemonic>duration</mnemonic></col>
    <col><mnemonic>event-label</mnemonic></col></schema>
    <row><start-time>100</start-time><duration>30</duration>
    <formatted-label fmt="topdown GPU probe pass 0:burn compute pass 0"/></row>
    </node></trace-query-result>"""
    summary = MODULE.summarize(counters, gpu)
    means = summary["sampled_counter_means"]
    assert means["ALU Limiter"] == {
        "value": 23.333333,
        "unit": "%",
        "sampled_time_ns": 30,
        "counter_intervals": 2,
    }
    assert means["GPU Read Bandwidth"]["unit"] == "GB/s"
    assert summary["probe_coverage"]["passes_with_counter_samples_by_metric"] == {
        "ALU Limiter": 1, "GPU Read Bandwidth": 1, "Top Performance Limiter": 1,
    }
    assert summary["top_limiter_categories"] == [{
        "name": "ALU Limiter",
        "sampled_time_ns": 30,
        "share_of_top_limiter_samples_percent": 100.0,
    }]
    assert summary["top_limiter_unparsed_sampled_time_ns"] == 0


def test_summary_reports_unparsed_top_limiter_time():
    counters = b"""<trace-query-result><node><schema name="metal-gpu-counter-intervals">
    <col><mnemonic>start</mnemonic></col><col><mnemonic>duration</mnemonic></col>
    <col><mnemonic>counter-id</mnemonic></col><col><mnemonic>name</mnemonic></col>
    <col><mnemonic>label</mnemonic></col><col><mnemonic>value</mnemonic></col>
    <col><mnemonic>is-percentage</mnemonic></col></schema>
    <row><start-time>100</start-time><duration>10</duration><uint32>0</uint32>
    <gpu-counter-name fmt="Top Performance Limiter">-2.Top Performance Limiter</gpu-counter-name>
    <formatted-label fmt="30% (1.ALU Limiter)"/><fixed-decimal>30</fixed-decimal><boolean>1</boolean></row>
    <row><start-time>110</start-time><duration>10</duration><uint32>0</uint32>
    <gpu-counter-name fmt="Top Performance Limiter">-2.Top Performance Limiter</gpu-counter-name>
    <formatted-label fmt="no category"/><fixed-decimal>10</fixed-decimal><boolean>1</boolean></row>
    </node></trace-query-result>"""
    gpu = b"""<trace-query-result><node><schema name="metal-gpu-intervals">
    <col><mnemonic>start</mnemonic></col><col><mnemonic>duration</mnemonic></col>
    <col><mnemonic>event-label</mnemonic></col></schema>
    <row><start-time>100</start-time><duration>20</duration>
    <formatted-label fmt="topdown GPU probe pass 0:burn compute pass 0"/></row>
    </node></trace-query-result>"""
    summary = MODULE.summarize(counters, gpu)
    assert summary["top_limiter_categories"][0]["share_of_top_limiter_samples_percent"] == 100
    assert summary["top_limiter_unparsed_sampled_time_ns"] == 10
    assert summary["top_limiter_unparsed_clean_sampled_time_ns"] is None


def test_summary_requires_pid_for_concurrent_labeled_probes():
    counters = b'<trace-query-result><schema name="metal-gpu-counter-intervals"/></trace-query-result>'
    gpu = b"""<trace-query-result><node><schema name="metal-gpu-intervals">
    <col><mnemonic>start</mnemonic></col><col><mnemonic>duration</mnemonic></col>
    <col><mnemonic>event-label</mnemonic></col><col><mnemonic>state</mnemonic></col>
    <col><mnemonic>process</mnemonic></col></schema>
    <row><start-time>100</start-time><duration>20</duration>
    <formatted-label fmt="topdown GPU probe pass 0:burn compute pass 0"/>
    <gpu-state fmt="Active"/><process><pid>101</pid></process></row>
    <row><start-time>110</start-time><duration>20</duration>
    <formatted-label fmt="topdown GPU probe pass 0:burn compute pass 0"/>
    <gpu-state fmt="Active"/><process><pid>202</pid></process></row>
    </node></trace-query-result>"""
    with pytest.raises(ValueError, match="multiple labeled probe PIDs"):
        MODULE.summarize(counters, gpu)
    result = MODULE.summarize(counters, gpu, pid=101)["probe_coverage"]
    assert result["passes_observed"] == 1
    assert result["gpu_work_time_ns"] == 20
    assert result["other_active_gpu_time_overlapping_ns"] == 10
    assert result["selected_pid"] == 101
    with pytest.raises(ValueError, match="no labeled probe intervals"):
        MODULE.summarize(counters, gpu, pid=303)


def test_summary_identifies_other_activity_during_sampled_probe_time():
    counters = b"""<trace-query-result><node><schema name="metal-gpu-counter-intervals"/>
    <row><start-time>100</start-time><duration>10</duration><uint32>0</uint32>
    <gpu-counter-name>ALU Limiter</gpu-counter-name></row></node></trace-query-result>"""
    gpu = b"""<trace-query-result><node><schema name="metal-gpu-intervals">
    <col><mnemonic>start</mnemonic></col><col><mnemonic>duration</mnemonic></col>
    <col><mnemonic>event-label</mnemonic></col><col><mnemonic>state</mnemonic></col></schema>
    <row><start-time>100</start-time><duration>20</duration>
    <formatted-label fmt="topdown GPU probe pass 0:burn compute pass 0"/>
    <gpu-state fmt="Active"/></row>
    <row><start-time>105</start-time><duration>10</duration>
    <formatted-label fmt="other GPU activity"/><gpu-state fmt="Active"/></row>
    </node></trace-query-result>"""
    coverage = MODULE.summarize(counters, gpu)["probe_coverage"]
    assert coverage["other_active_gpu_time_overlapping_ns"] == 10
    assert coverage["sampled_gpu_work_time_ns"] == 10
    assert coverage["other_active_gpu_time_in_counter_samples_ns"] == 5
    assert coverage["observed_other_activity_fraction_of_sampled_gpu_work"] == 0.5


def test_summary_keeps_separate_means_without_observed_other_activity():
    counters = b"""<trace-query-result><node><schema name="metal-gpu-counter-intervals">
    <col><mnemonic>start</mnemonic></col><col><mnemonic>duration</mnemonic></col>
    <col><mnemonic>counter-id</mnemonic></col><col><mnemonic>name</mnemonic></col>
    <col><mnemonic>label</mnemonic></col><col><mnemonic>value</mnemonic></col>
    <col><mnemonic>is-percentage</mnemonic></col></schema>
    <row><start-time>100</start-time><duration>10</duration><uint32>0</uint32>
    <gpu-counter-name fmt="ALU Limiter">1.ALU Limiter</gpu-counter-name>
    <formatted-label fmt="10%"/><fixed-decimal>10</fixed-decimal><boolean>1</boolean></row>
    <row><start-time>110</start-time><duration>10</duration><uint32>0</uint32>
    <gpu-counter-name fmt="ALU Limiter">1.ALU Limiter</gpu-counter-name>
    <formatted-label fmt="90%"/><fixed-decimal>90</fixed-decimal><boolean>1</boolean></row>
    </node></trace-query-result>"""
    gpu = b"""<trace-query-result><node><schema name="metal-gpu-intervals">
    <col><mnemonic>start</mnemonic></col><col><mnemonic>duration</mnemonic></col>
    <col><mnemonic>event-label</mnemonic></col><col><mnemonic>state</mnemonic></col></schema>
    <row><start-time>100</start-time><duration>10</duration>
    <formatted-label fmt="topdown GPU probe pass 0:burn compute pass 0"/>
    <gpu-state fmt="Active"/></row>
    <row><start-time>110</start-time><duration>10</duration>
    <formatted-label fmt="topdown GPU probe pass 1:burn compute pass 1"/>
    <gpu-state fmt="Active"/></row>
    <row><start-time>105</start-time><duration>5</duration>
    <formatted-label fmt="other GPU activity"/><gpu-state fmt="Active"/></row>
    </node></trace-query-result>"""
    result = MODULE.summarize(counters, gpu)
    assert result["sampled_counter_means"]["ALU Limiter"]["value"] == 50
    clean = result["sampled_counter_means_without_observed_other_activity"]["ALU Limiter"]
    assert clean["value"] == 90
    assert clean["sampled_time_ns"] == 10
    assert clean["counter_intervals"] == 1
    assert result["sampled_per_pass_metric_distributions"]["ALU Limiter"]["sampled_passes"] == 2
    assert result["sampled_per_pass_metric_distributions_without_observed_other_activity"]["ALU Limiter"] == {
        "sampled_passes": 1, "unit": "%", "minimum": 90, "p10": 90,
        "median": 90, "p90": 90, "maximum": 90,
    }


def test_summary_streams_files_and_keeps_cross_row_and_nested_references(tmp_path):
    counters = tmp_path / "counters.xml"
    counters.write_bytes(b"""<trace-query-result><node><schema name="metal-gpu-counter-intervals">
    <col><mnemonic>start</mnemonic></col><col><mnemonic>duration</mnemonic></col>
    <col><mnemonic>counter-id</mnemonic></col><col><mnemonic>name</mnemonic></col>
    <col><mnemonic>label</mnemonic></col><col><mnemonic>value</mnemonic></col>
    <col><mnemonic>is-percentage</mnemonic></col></schema>
    <row><start-time id="1">100</start-time><duration id="2">10</duration><uint32>0</uint32>
    <gpu-counter-name id="4" fmt="ALU Limiter">1.ALU Limiter</gpu-counter-name>
    <formatted-label><fixed-decimal id="6">5</fixed-decimal></formatted-label>
    <fixed-decimal ref="6"/><boolean id="7">1</boolean></row>
    <row><start-time>110</start-time><duration ref="2"/><uint32>0</uint32>
    <gpu-counter-name ref="4"/><formatted-label/>
    <fixed-decimal ref="6"/><boolean ref="7"/></row>
    </node></trace-query-result>""")
    gpu = tmp_path / "gpu.xml"
    gpu.write_bytes(b"""<trace-query-result><node><schema name="metal-gpu-intervals">
    <col><mnemonic>start</mnemonic></col><col><mnemonic>duration</mnemonic></col>
    <col><mnemonic>event-label</mnemonic></col><col><mnemonic>state</mnemonic></col>
    <col><mnemonic>process</mnemonic></col></schema>
    <row><start-time>100</start-time><duration>10</duration>
    <formatted-label fmt="topdown GPU probe pass 0:burn compute pass 0"/>
    <gpu-state fmt="Active"/><process id="21"><pid id="22">101</pid></process></row>
    <row><start-time>110</start-time><duration>10</duration>
    <formatted-label fmt="topdown GPU probe pass 1:burn compute pass 1"/>
    <gpu-state fmt="Active"/><process ref="21"/></row>
    </node></trace-query-result>""")
    result = MODULE.summarize(counters, gpu)
    assert result["probe_coverage"]["passes_observed"] == 2
    assert result["sampled_counter_means"]["ALU Limiter"]["value"] == 5.0


@pytest.mark.parametrize("source,message", [
    (b'<trace-query-result><schema name="metal-gpu-counter-intervals"/>'
     b'<row><start-time id="1">1</start-time><duration>1</duration><uint32>0</uint32>'
     b'<gpu-counter-name>A</gpu-counter-name></row>'
     b'<row><start-time id="1">2</start-time><duration>1</duration><uint32>0</uint32>'
     b'<gpu-counter-name>A</gpu-counter-name></row></trace-query-result>', "duplicate XML id"),
    (b'<trace-query-result><schema name="metal-gpu-counter-intervals"/>'
     b'<row><start-time>1</start-time><duration>0</duration><uint32>0</uint32>'
     b'<gpu-counter-name>A</gpu-counter-name></row></trace-query-result>', "duration must be positive"),
])
def test_summary_rejects_ambiguous_or_zero_length_samples(source, message):
    with pytest.raises(ValueError, match=message):
        MODULE.summarize(source)
