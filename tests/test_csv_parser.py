"""Tests for toplev CSV parser."""

from pathlib import Path

from topdown.collector.csv_parser import (
    CsvFormat,
    detect_format,
    parse_cpu,
    parse_output,
    parse_value,
)

TEST_DATA = Path(__file__).parent / "test_data"


class TestParseValue:
    def test_percentage_with_suffix(self):
        assert parse_value("15.87%") == 15.87

    def test_plain_float(self):
        assert parse_value("15.87") == 15.87

    def test_zero(self):
        assert parse_value("0.0") == 0.0

    def test_invalid(self):
        assert parse_value("N/A") == 0.0


class TestParseCpu:
    def test_plain_int(self):
        assert parse_cpu("0") == 0
        assert parse_cpu("3") == 3

    def test_cpu_prefix(self):
        assert parse_cpu("CPU0") == 0
        assert parse_cpu("CPU12") == 12

    def test_socket_core(self):
        assert parse_cpu("S0-C0") == 0
        assert parse_cpu("S0-C3") == 3

    def test_socket_only(self):
        assert parse_cpu("S0") == 0

    def test_invalid(self):
        assert parse_cpu("unknown") is None


class TestDetectFormat:
    def test_basic_4col(self):
        lines = ["1.001,Frontend_Bound,15.2%,%"]
        assert detect_format(lines) == CsvFormat.BASIC_4COL

    def test_basic_5col(self):
        lines = ["0.200,Frontend_Bound,15.87,%,"]
        assert detect_format(lines) == CsvFormat.BASIC_5COL

    def test_percpu_5col(self):
        lines = ["1.001,0,Frontend_Bound,12.3,%"]
        assert detect_format(lines) == CsvFormat.PERCPU_5COL

    def test_percpu_6col(self):
        lines = ["1.001,0,Frontend_Bound,12.3,%,below"]
        assert detect_format(lines) == CsvFormat.PERCPU_6COL

    def test_snapshot_3col(self):
        lines = ["Frontend_Bound,15.2,%"]
        assert detect_format(lines) == CsvFormat.SNAPSHOT_3COL

    def test_empty(self):
        assert detect_format([]) == CsvFormat.UNKNOWN

    def test_comments_skipped(self):
        lines = ["# header comment", "1.001,Frontend_Bound,15.2%,%"]
        assert detect_format(lines) == CsvFormat.BASIC_4COL


class TestParseOutput:
    def test_basic_csv(self):
        text = (TEST_DATA / "toplev_csv_basic.csv").read_text()
        samples = parse_output(text)
        assert len(samples) == 12
        assert samples[0].metric_name == "Frontend_Bound"
        assert samples[0].value == 15.2
        assert samples[0].cpu is None
        assert samples[0].timestamp == 1.001131873

    def test_percpu_csv(self):
        text = (TEST_DATA / "toplev_csv_percpu.csv").read_text()
        samples = parse_output(text)
        assert len(samples) == 18
        # CPU 0 samples
        cpu0 = [s for s in samples if s.cpu == 0]
        assert len(cpu0) == 9
        # CPU 1 samples
        cpu1 = [s for s in samples if s.cpu == 1]
        assert len(cpu1) == 9
        # Check hierarchical metric
        dram = [s for s in samples if s.metric_name == "Backend_Bound.Memory_Bound.DRAM_Bound"]
        assert len(dram) == 2

    def test_system_wide_csv(self):
        text = (TEST_DATA / "toplev_csv_system_wide.csv").read_text()
        samples = parse_output(text)
        assert len(samples) == 22
        # All L1 metrics present
        l1_names = {s.metric_name for s in samples if "." not in s.metric_name}
        assert l1_names == {"Frontend_Bound", "Bad_Speculation", "Backend_Bound", "Retiring"}
        # Check a status field
        itlb = [s for s in samples if "ITLB" in s.metric_name]
        assert len(itlb) == 1
        assert itlb[0].status == "below"

    def test_metric_level_inference(self):
        text = (TEST_DATA / "toplev_csv_system_wide.csv").read_text()
        samples = parse_output(text)
        l1 = [s for s in samples if s.level == 1]
        l2 = [s for s in samples if s.level == 2]
        l3 = [s for s in samples if s.level == 3]
        assert len(l1) == 4
        assert len(l2) > 0
        assert len(l3) > 0

    def test_empty_input(self):
        assert parse_output("") == []
        assert parse_output("# just comments\n# more comments") == []
