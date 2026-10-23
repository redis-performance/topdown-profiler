"""Deterministic workload controls for future M1 counter calibration."""

import json
import platform
import shutil
import subprocess
import sys
from pathlib import Path
from runpy import run_path

import pytest


SOURCE = Path(__file__).resolve().parents[1] / "examples/apple-m1-controls.c"
ICACHE_SOURCE = Path(__file__).resolve().parents[1] / "examples/apple-m1-icache.c"
PLANNER = Path(__file__).resolve().parents[1] / "examples/apple-m1-kpep-plan.py"
BRANCH_NOP_SOURCE = Path(__file__).resolve().parents[1] / "examples/apple-m1-branch-nop.c"
SIMD_SOURCE = Path(__file__).resolve().parents[1] / "examples/apple-m1-simd.c"
DIVIDE_SOURCE = Path(__file__).resolve().parents[1] / "examples/apple-m1-divide.c"
MIXED_CORE_SOURCE = Path(__file__).resolve().parents[1] / "examples/apple-m1-mixed-core.c"


@pytest.fixture(scope="module")
def controls(tmp_path_factory):
    if platform.system() != "Darwin" or platform.machine() != "arm64" or not shutil.which("clang"):
        pytest.skip("M1 workload controls require Apple silicon and clang")
    output = tmp_path_factory.mktemp("apple-m1-controls") / "controls"
    subprocess.run(["clang", "-O3", str(SOURCE), "-o", str(output)], check=True, timeout=30)
    return output


@pytest.mark.parametrize("args,expected", [
    (("serial", "100"), "1838323455979401253"),
    (("parallel", "100"), "15343226476468928520"),
    (("alu_wide", "100"), "3636"),
    (("branch_predictable", "100"), "18127535559106838743"),
    (("branch_random", "100"), "18127535559106838807"),
    (("memory", "100", "1024"), "985"),
])
def test_controls_complete_with_deterministic_checksums(controls, args, expected):
    result = subprocess.run([str(controls), *args], capture_output=True, text=True, timeout=5, check=True)
    assert result.stdout.strip() == expected
    assert result.stderr == ""


@pytest.mark.parametrize("args", [(), ("unknown", "100"), ("memory", "100", "1")])
def test_invalid_control_arguments_exit_without_running(controls, args):
    result = subprocess.run([str(controls), *args], capture_output=True, text=True, timeout=5)
    assert result.returncode == 2


@pytest.mark.parametrize("nop_count", [8, 16, 64])
def test_mixed_branch_nop_control_keeps_the_same_result(tmp_path, nop_count):
    if platform.system() != "Darwin" or platform.machine() != "arm64" or not shutil.which("clang"):
        pytest.skip("M1 workload controls require Apple silicon and clang")
    output = tmp_path / "branch-nop"
    subprocess.run(["clang", "-O3", f"-DNOP_COUNT={nop_count}", str(BRANCH_NOP_SOURCE), "-o", str(output)],
                   check=True, timeout=30)
    result = subprocess.run([str(output), "1000"], capture_output=True, text=True, timeout=5, check=True)
    assert result.stdout.strip() == "795230588156886778"


def test_simd_control_compiles_and_completes(tmp_path):
    if platform.system() != "Darwin" or platform.machine() != "arm64" or not shutil.which("clang"):
        pytest.skip("M1 workload controls require Apple silicon and clang")
    output = tmp_path / "simd"
    subprocess.run(["clang", "-O3", str(SIMD_SOURCE), "-o", str(output)], check=True, timeout=30)
    result = subprocess.run([str(output), "1000"], capture_output=True, text=True, timeout=5, check=True)
    assert result.stdout.strip() == "1000"


def test_divide_control_executes_udiv_with_a_stable_result(tmp_path):
    if (platform.system() != "Darwin" or platform.machine() != "arm64"
            or not shutil.which("clang") or not shutil.which("otool")):
        pytest.skip("M1 divide control requires Apple silicon compiler tools")
    output = tmp_path / "divide"
    subprocess.run(["clang", "-O3", str(DIVIDE_SOURCE), "-o", str(output)], check=True, timeout=30)
    instruction_text = subprocess.check_output(["otool", "-tvV", str(output)], text=True, timeout=30)
    assert "udiv" in instruction_text
    result = subprocess.run([str(output), "1000"], capture_output=True, text=True, timeout=5, check=True)
    assert result.stdout.strip() == "7561112086505667263"
    assert subprocess.run([str(output), "0"], capture_output=True, timeout=5).returncode == 2


def test_mixed_core_control_compiles_and_completes(tmp_path):
    if platform.system() != "Darwin" or platform.machine() != "arm64" or not shutil.which("clang"):
        pytest.skip("M1 workload controls require Apple silicon and clang")
    output = tmp_path / "mixed-core"
    subprocess.run(["clang", "-O3", str(MIXED_CORE_SOURCE), "-o", str(output)], check=True, timeout=30)
    result = subprocess.run([str(output), "1000", "1000"], capture_output=True, text=True,
                            timeout=5, check=True)
    assert result.stdout.strip() == "795230588156917206"


def test_calibration_uses_map_bubble_delivery_floor(tmp_path):
    validator = Path(__file__).resolve().parents[1] / "examples/apple-m1-validate-tma.py"
    validate = run_path(str(validator))["validate"]
    metrics = tmp_path / "metrics.xml"
    counters = tmp_path / "counters.xml"
    columns = {"timestamp": "start-time", "duration": "duration", "pmi-event": "string",
               "metric-display-name": "string", "thread": "thread", "process": "process",
               "metric-value": "fixed-decimal", "core": "core", "is-ratio": "boolean",
               "documentation": "markdown-text"}
    header = "".join(f"<col><mnemonic>{name}</mnemonic><engineering-type>{kind}</engineering-type></col>"
                     for name, kind in columns.items())
    rows = []
    for event, value in (("cycle", 1000), ("delivery", 0.105), ("discarded", 0.2),
                         ("processing", 0.2), ("useful", 0.5)):
        rows.append(f'<row><start-time>100</start-time><duration>1000</duration>'
                    f'<string>{event}</string><string>{event}</string>'
                    '<thread><tid>2</tid><process><pid>42</pid></process></thread>'
                    f'<process><pid>42</pid></process><fixed-decimal>{value}</fixed-decimal>'
                    f'<core fmt="CPU 7">7</core><boolean>{int(event != "cycle")}</boolean>'
                    '<sentinel/></row>')
    metrics.write_text(f'<trace-query-result><node><schema name="MetricTable">{header}</schema>'
                       f'{"".join(rows)}</node></trace-query-result>')
    counters.write_text('<trace-query-result><node><schema name="counters-profile">'
                        '<col><mnemonic>time</mnemonic></col><col><mnemonic>thread</mnemonic></col>'
                        '<col><mnemonic>process</mnemonic></col><col><mnemonic>core</mnemonic></col>'
                        '<col><mnemonic>counters-array</mnemonic></col></schema><row>'
                        '<sample-time>1100</sample-time>'
                        '<thread><tid>2</tid><process><pid>42</pid></process></thread>'
                        '<process><pid>42</pid></process><core>7</core>'
                        '<pmc-events>1000 4000 1000 100 200 4800 0 4000 0 105 0 0</pmc-events>'
                        '</row></node></trace-query-result>')
    result = validate(counters, metrics, 42)
    assert result["matched_intervals"] == 1
    assert result["guided_intervals"] == 1
    assert result["unmatched_raw_samples"] == result["unmatched_guided_intervals"] == 0
    assert result["matched_guided_cycle_fraction"] == 1
    assert result["guided_threads_observed_on_both_core_kinds"] == 0
    assert result["guided_core_kind_transitions_observed"] == 0
    assert result["by_core_kind"]["P"]["matched_guided_cycle_fraction"] == 1
    assert result["alternative_width_useful_error_by_core_kind"]["P"]["assumed_width"] == 4
    assert result["alternative_width_useful_error_by_core_kind"]["P"][
        "median_absolute_difference_percentage_points"] == pytest.approx(50)
    assert result["aggregated_raw_windows"]["by_core_kind"]["P"]["guided_cycles"] == 1000
    assert result["matches_exported_rounding"] is True
    counter_text = counters.read_text()
    duplicate = "<row>" + counter_text.split("<row>", 1)[1].split("</row>", 1)[0] + "</row>"
    metric_text = metrics.read_text()
    e_rows = metric_text.split("</schema>", 1)[1].split("</node>", 1)[0]
    e_rows = e_rows.replace("<start-time>100</start-time>", "<start-time>2000</start-time>")
    e_rows = e_rows.replace('<core fmt="CPU 7">7</core>', '<core fmt="CPU 0">0</core>')
    e_raw = duplicate.replace("<sample-time>1100</sample-time>", "<sample-time>3000</sample-time>")
    e_raw = e_raw.replace("<core>7</core>", "<core>0</core>")
    e_raw = e_raw.replace("200 4800 0 4000", "200 2400 0 2000")
    metrics.write_text(metric_text.replace("</node>", e_rows + "</node>"))
    counters.write_text(counter_text.replace("</node>", e_raw + "</node>"))
    migrated = validate(counters, metrics, 42)
    assert migrated["guided_threads_observed_on_both_core_kinds"] == 1
    assert migrated["guided_core_kind_transitions_observed"] == 1
    assert {kind: row["matched_intervals"] for kind, row in migrated["by_core_kind"].items()} == {"E": 1, "P": 1}
    assert migrated["alternative_width_useful_error_by_core_kind"]["E"][
        "median_absolute_difference_percentage_points"] == pytest.approx(25)
    assert migrated["matches_exported_rounding"] is True
    metrics.write_text(metric_text)
    counters.write_text(counter_text)
    metrics.write_text(metric_text.replace('<core fmt="CPU 7">7</core>', '<core fmt="CPU 8">8</core>'))
    with pytest.raises(ValueError, match="guided core"):
        validate(counters, metrics, 42)
    metrics.write_text(metric_text)
    counters.write_text(counter_text.replace("1000 4000 1000", "-1 4000 1000"))
    with pytest.raises(ValueError, match="Negative M1 counter"):
        validate(counters, metrics, 42)
    counters.write_text(counter_text)
    counters.write_text(counter_text.replace("</node>", duplicate + "</node>"))
    with pytest.raises(ValueError, match="duplicate end-time"):
        validate(counters, metrics, 42)
    counters.write_text(counter_text.replace("</node>",
                                            duplicate.replace("4000 1000 100", "4000 900 100") + "</node>"))
    with pytest.raises(ValueError, match="duplicate end-time"):
        validate(counters, metrics, 42)
    counters.write_text(counter_text)
    extra_rows = metric_text.split("</schema>", 1)[1].split("</node>", 1)[0]
    metrics.write_text(metric_text.replace("</node>", extra_rows.replace("<start-time>100</start-time>",
                                                            "<start-time>200</start-time>") + "</node>"))
    partial = validate(counters, metrics, 42)
    assert partial["guided_intervals"] == 2
    assert partial["unmatched_guided_intervals"] == 1
    assert partial["matched_guided_cycle_fraction"] == 0.5
    assert partial["unmatched_guided_intervals_by_reason"] == {"no_raw_counter_sample_at_end_time": 1}
    assert partial["unmatched_guided_cycles_by_reason"] == {"no_raw_counter_sample_at_end_time": 1000}
    mismatch = duplicate.replace("<sample-time>1100</sample-time>", "<sample-time>1200</sample-time>")
    mismatch = mismatch.replace("1000 4000 1000 100 200", "1000 4000 900 100 200")
    counters.write_text(counter_text.replace("</node>", mismatch + "</node>"))
    partial = validate(counters, metrics, 42)
    assert partial["unmatched_guided_intervals_by_reason"] == {"active_cycle_count_mismatch": 1}
    assert partial["unmatched_guided_cycles_by_reason"] == {"active_cycle_count_mismatch": 1000}
    wrong_core = duplicate.replace("<sample-time>1100</sample-time>", "<sample-time>1200</sample-time>")
    wrong_core = wrong_core.replace('<core>7</core>', '<core>6</core>')
    counters.write_text(counter_text.replace("</node>", wrong_core + "</node>"))
    partial = validate(counters, metrics, 42)
    assert partial["unmatched_guided_intervals_by_reason"] == {"raw_counter_scope_mismatch": 1}
    counters.write_text(counter_text)
    metrics.write_text(metric_text)
    metrics.write_text(metrics.read_text().replace("<fixed-decimal>0.105</fixed-decimal>",
                                                   "<fixed-decimal>0.1</fixed-decimal>"))
    assert validate(counters, metrics, 42)["matches_exported_rounding"] is False
    counters.write_text(counters.read_text().replace("4000 0 105 0 0", "4000 0 105"))
    with pytest.raises(ValueError, match="vector layout"):
        validate(counters, metrics, 42)
    metric_rows = metric_text.split("</schema>", 1)[1].split("</node>", 1)[0]
    first_half = metric_rows.replace("<duration>1000</duration>", "<duration>500</duration>")
    first_half = first_half.replace("<fixed-decimal>1000</fixed-decimal>",
                                    "<fixed-decimal>500</fixed-decimal>")
    second_half = first_half.replace("<start-time>100</start-time>", "<start-time>600</start-time>")
    metrics.write_text(metric_text.replace(metric_rows, first_half + second_half))
    prior = duplicate.replace("<sample-time>1100</sample-time>", "<sample-time>100</sample-time>")
    counters.write_text(counter_text.replace("</node>", prior + "</node>"))
    split_only = validate(counters, metrics, 42)
    assert split_only["matched_intervals"] == 0
    assert split_only["matches_exported_rounding"] is False
    assert split_only["aggregated_raw_windows"]["cycle_aligned_multi_interval_windows"] == 1
    assert split_only["aggregated_raw_windows"]["matches_exported_rounding"] is True


def test_calibration_checks_multiple_guided_intervals_per_raw_window():
    validator = Path(__file__).resolve().parents[1] / "examples/apple-m1-validate-tma.py"
    aggregate = run_path(str(validator))["_aggregate_windows"]
    values = [1000, 4000, 1000, 100, 200, 4800, 0, 4000, 0, 105, 0, 0]
    shares = {"useful": 0.5, "processing": 0.2, "discarded": 0.2, "delivery": 0.105}
    guided = [
        {"time_ns": 100, "duration_ns": 500, "tid": 2, "core": 7, "cycles": 500, "shares": shares},
        {"time_ns": 600, "duration_ns": 500, "tid": 2, "core": 7, "cycles": 500, "shares": shares},
    ]
    raw = {(2, 7): [(100, values), (1100, values)]}
    result = aggregate(raw, guided, 1000)
    assert result["cycle_aligned_windows"] == 1
    assert result["cycle_aligned_multi_interval_windows"] == 1
    assert result["windows_matching_exported_rounding"] == 1
    assert result["guided_cycle_fraction_in_cycle_aligned_windows"] == 1
    assert result["matches_exported_rounding"] is True
    guided[1]["cycles"] = 400
    assert aggregate(raw, guided, 900)["windows_with_cycle_mismatch"] == 1
    guided[1]["cycles"] = 500
    guided[0]["time_ns"] = 50
    assert aggregate(raw, guided, 1000)["windows_with_boundary_straddle"] == 1
    guided[0]["time_ns"] = 100
    guided[1]["time_ns"] = 550
    assert aggregate(raw, guided, 1000)["windows_with_overlapping_guided_intervals"] == 1


def test_icache_control_matches_hot_and_large_instruction_streams(tmp_path):
    if (platform.system() != "Darwin" or platform.machine() != "arm64"
            or not shutil.which("clang") or not shutil.which("otool")):
        pytest.skip("M1 instruction-cache control requires Apple silicon and clang")
    output = tmp_path / "icache"
    hot = tmp_path / "icache-hot"
    medium = tmp_path / "icache-medium"
    custom = tmp_path / "icache-custom"
    cold = tmp_path / "icache-cold"
    subprocess.run(["clang", "-O3", "-g", str(ICACHE_SOURCE), "-o", str(output)], check=True, timeout=30)
    subprocess.run(["clang", "-O3", "-g", "-DTOPDOWN_SMALL_ICACHE", str(ICACHE_SOURCE), "-o", str(hot)],
                   check=True, timeout=30)
    subprocess.run(["clang", "-O3", "-g", "-DTOPDOWN_NOP_BLOCK=32768", str(ICACHE_SOURCE), "-o", str(medium)],
                   check=True, timeout=30)
    subprocess.run(["clang", "-O3", "-g", "-DTOPDOWN_NOP_BLOCK=40960", "-DTOPDOWN_NOP_GROUPS=1",
                    str(ICACHE_SOURCE), "-o", str(custom)], check=True, timeout=30)
    subprocess.run(["clang", "-O3", "-g", "-DTOPDOWN_SMALL_ICACHE", "-DTOPDOWN_COLD_NOP_BLOCK=65536",
                    str(ICACHE_SOURCE), "-o", str(cold)], check=True, timeout=30)

    def text_bytes(binary):
        result = subprocess.run(["otool", "-l", str(binary)], capture_output=True, text=True, check=True)
        return int(result.stdout.split("sectname __text", 1)[1].split("size 0x", 1)[1].split()[0], 16)

    assert text_bytes(hot) < 131072 < text_bytes(medium) < text_bytes(custom) < 196608 < text_bytes(output)
    assert text_bytes(cold) > text_bytes(output)
    for binary in (output, hot, medium, custom, cold):
        result = subprocess.run([str(binary), "2"], capture_output=True, text=True, timeout=5, check=True)
        assert result.stdout.strip() == str(2 ^ 0x1CACED)
    for args in ((), ("0",), ("-1",), ("invalid",)):
        result = subprocess.run([str(output), *args], capture_output=True, text=True, timeout=5)
        assert result.returncode == 2


@pytest.mark.parametrize("group,expected_events", [
    ("controls", {"RETIRE_UOP", "BRANCH_MISPRED_NONSPEC", "SCHEDULE_UOP",
                  "CORE_ACTIVE_CYCLE", "MAP_DISPATCH_BUBBLE", "MAP_STALL_DISPATCH",
                  "L1D_CACHE_MISS_LD", "L1I_CACHE_MISS_DEMAND"}),
    ("map-state", {"RETIRE_UOP", "MAP_DISPATCH_BUBBLE", "MAP_STALL", "MAP_REWIND",
                   "MAP_STALL_DISPATCH", "MAP_INT_UOP", "MAP_LDST_UOP", "MAP_SIMD_UOP"}),
    ("fetch", {"RETIRE_UOP", "CORE_ACTIVE_CYCLE", "MAP_DISPATCH_BUBBLE", "SCHEDULE_EMPTY",
               "L1I_CACHE_MISS_DEMAND", "L1I_TLB_MISS_DEMAND", "L2_TLB_MISS_INSTRUCTION", "FETCH_RESTART"}),
    ("calibration", {"RETIRE_UOP", "MAP_DISPATCH_BUBBLE", "MAP_STALL", "MAP_REWIND",
                     "CORE_ACTIVE_CYCLE", "MAP_INT_UOP", "MAP_LDST_UOP", "MAP_SIMD_UOP"}),
])
def test_m1_kpep_plan_uses_distinct_hardware_counters(group, expected_events):
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        pytest.skip("KPEP requires Apple silicon")
    family = subprocess.check_output(["sysctl", "-n", "hw.cpufamily"], text=True).strip()
    if int(family) != 0x1B588BB3:
        pytest.skip("This event plan is for M1")
    result = subprocess.run([sys.executable, str(PLANNER), "--group", group],
                            capture_output=True, text=True, timeout=10, check=True)
    data = json.loads(result.stdout)
    mapping = data["event_to_counter"]
    assert data["group"] == group
    assert mapping["RETIRE_UOP"] == 7
    assert set(mapping) == {"FIXED_CYCLES", "FIXED_INSTRUCTIONS", *expected_events}
    assert set(mapping.values()) == set(range(10))
    assert data["hardware_access_verified"] is False


def test_m1_kpep_plan_rejects_invalid_event_swap_before_hardware_access():
    plan = run_path(str(PLANNER))["plan"]
    with pytest.raises(ValueError, match="distinct programmable events"):
        plan("calibration", ("FIXED_CYCLES", "RETIRE_UOP"))
    with pytest.raises(ValueError, match="distinct programmable events"):
        plan("calibration", ("RETIRE_UOP", "RETIRE_UOP"))


def test_m1_kpep_plan_checks_swapped_calibration_order():
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        pytest.skip("KPEP requires Apple silicon")
    family = subprocess.check_output(["sysctl", "-n", "hw.cpufamily"], text=True).strip()
    if int(family) != 0x1B588BB3:
        pytest.skip("This event plan is for M1")
    result = subprocess.run([sys.executable, str(PLANNER), "--group", "calibration",
                             "--swap", "RETIRE_UOP", "MAP_DISPATCH_BUBBLE"],
                            capture_output=True, text=True, timeout=10, check=True)
    data = json.loads(result.stdout)
    assert data["requested_events"][2:4] == ["MAP_DISPATCH_BUBBLE", "RETIRE_UOP"]
    assert data["event_to_counter"]["RETIRE_UOP"] == 7
    assert set(data["event_to_counter"].values()) == set(range(10))
