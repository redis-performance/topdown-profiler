"""Tests for CLI commands using Typer CliRunner."""

import csv
import io
import json

import pytest
from typer.testing import CliRunner

from tests.conftest import INTEL_RUN_A, INTEL_RUN_B, load_intel_runs
from topdown.cli import app
from topdown.storage.models import Run, Sample
from topdown.storage.sqlite_backend import SQLiteBackend

runner = CliRunner()


@pytest.fixture
def db_with_data(tmp_path):
    """Create a populated database and return its path."""
    db_path = tmp_path / "cli_test.db"
    backend = SQLiteBackend(db_path=db_path)
    backend.initialize()

    run = Run(
        run_id="cli-test-001",
        process_name="redis-server",
        level=2,
        labels={
            "git_branch": "unstable",
            "test_name": "set-get-100",
            "topology": "oss-standalone",
        },
    )
    backend.insert_run(run)

    samples = [
        Sample(run_id="cli-test-001", timestamp=1.0, metric_name="Frontend_Bound", value=15.0, unit="%"),
        Sample(run_id="cli-test-001", timestamp=1.0, metric_name="Backend_Bound", value=45.0, unit="%"),
        Sample(run_id="cli-test-001", timestamp=1.0, metric_name="Backend_Bound.Memory_Bound", value=30.0, unit="%"),
        Sample(run_id="cli-test-001", timestamp=1.0, metric_name="Backend_Bound.Memory_Bound.DRAM_Bound", value=18.0, unit="%"),
        Sample(run_id="cli-test-001", timestamp=1.0, metric_name="Backend_Bound.Core_Bound", value=15.0, unit="%"),
        Sample(run_id="cli-test-001", timestamp=1.0, metric_name="Bad_Speculation", value=10.0, unit="%"),
        Sample(run_id="cli-test-001", timestamp=1.0, metric_name="Retiring", value=30.0, unit="%"),
    ]
    backend.insert_samples(samples)
    backend.close()
    return str(db_path)


class TestVersionCommand:
    def test_version(self):
        result = runner.invoke(app, ["version"])
        assert result.exit_code == 0
        assert "topdown-profiler" in result.output


class TestListCommand:
    def test_list_no_data(self, tmp_path):
        db_path = str(tmp_path / "empty.db")
        result = runner.invoke(app, ["list", "--db", db_path, "--last", "24h"])
        assert result.exit_code == 0
        assert "No runs found" in result.output

    def test_list_with_data(self, db_with_data):
        result = runner.invoke(app, ["list", "--db", db_with_data, "--last", "24h"])
        assert result.exit_code == 0
        assert "cli-test-001" in result.output or "redis-server" in result.output

    def test_list_json(self, db_with_data):
        result = runner.invoke(app, ["list", "--db", db_with_data, "--json", "--last", "24h"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert isinstance(data, list)
        assert len(data) >= 1

    def test_list_filter_by_label(self, db_with_data):
        result = runner.invoke(app, ["list", "--db", db_with_data, "--label", "git_branch=unstable", "--last", "24h"])
        assert result.exit_code == 0
        assert "redis-server" in result.output

    def test_list_filter_by_label_no_match(self, db_with_data):
        result = runner.invoke(app, ["list", "--db", db_with_data, "--label", "git_branch=nonexistent", "--last", "24h"])
        assert result.exit_code == 0
        assert "No runs found" in result.output


class TestQueryCommand:
    def test_query_bottlenecks(self, db_with_data):
        result = runner.invoke(app, ["query", "--db", db_with_data, "--bottlenecks", "--last", "24h"])
        assert result.exit_code == 0
        assert "Backend_Bound" in result.output

    def test_query_tree(self, db_with_data):
        result = runner.invoke(app, ["query", "--db", db_with_data, "--tree", "--last", "24h"])
        assert result.exit_code == 0
        assert "Backend_Bound" in result.output

    def test_query_funnel(self, db_with_data):
        result = runner.invoke(app, ["query", "--db", db_with_data, "--funnel", "--last", "24h"])
        assert result.exit_code == 0
        assert "Funnel" in result.output or "Useful" in result.output

    def test_query_by_bottleneck(self, db_with_data):
        result = runner.invoke(app, ["query", "--db", db_with_data, "--bottleneck", "DRAM_Bound", "--min-pct", "10", "--last", "24h"])
        assert result.exit_code == 0
        # Rich table truncates column content; check for partial match
        assert "Bottleneck" in result.output or "cli-test" in result.output or "18.0" in result.output

    def test_query_json(self, db_with_data):
        # Use --bottleneck with --json to get parseable JSON
        result = runner.invoke(app, ["query", "--db", db_with_data, "--bottleneck", "DRAM_Bound", "--min-pct", "10", "--json", "--last", "24h"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert isinstance(data, list)

    def test_query_by_run_id(self, db_with_data):
        result = runner.invoke(app, ["query", "--db", db_with_data, "--run-id", "cli-test-001", "--bottlenecks", "--last", "24h"])
        assert result.exit_code == 0

    def test_query_with_label_filter(self, db_with_data):
        result = runner.invoke(app, [
            "query", "--db", db_with_data,
            "--label", "git_branch=unstable",
            "--bottlenecks", "--last", "24h",
        ])
        assert result.exit_code == 0


class TestExplainCommand:
    def test_explain_metric(self):
        result = runner.invoke(app, ["explain", "Backend_Bound"])
        assert result.exit_code == 0
        assert "Backend" in result.output

    def test_explain_partial_name(self):
        result = runner.invoke(app, ["explain", "DRAM_Bound"])
        assert result.exit_code == 0
        assert "DRAM" in result.output or "memory" in result.output.lower()

    def test_explain_unknown(self):
        result = runner.invoke(app, ["explain", "FakeMetric123"])
        assert result.exit_code == 1

    def test_explain_json(self):
        result = runner.invoke(app, ["explain", "Frontend_Bound", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert "description" in data


class TestCompareCommand:
    def test_compare_requires_args(self):
        result = runner.invoke(app, ["compare"])
        # Should fail without run IDs or labels
        assert result.exit_code != 0 or "Error" in result.output

    def test_compare_nonexistent_run(self, db_with_data):
        result = runner.invoke(app, ["compare", "nonexistent-a", "nonexistent-b", "--db", db_with_data])
        assert result.exit_code == 1 or "not found" in result.output


class TestInstallServiceCommand:
    def test_preview(self):
        result = runner.invoke(app, [
            "install-service", "--process", "redis-server",
            "--level", "2", "--every", "5m", "--preview",
        ])
        assert result.exit_code == 0
        assert "ExecStart" in result.output
        assert "redis-server" in result.output
        assert "systemd" in result.output.lower() or "[Unit]" in result.output


class _IntelCli:
    """Shared fixtures: Intel-style runs and a width-pinned CLI invoker.

    Every call pins the Rich consoles to an explicit width: they are created at
    import time, so COLUMNS alone would depend on test order.
    """

    RUN_A = INTEL_RUN_A
    RUN_B = INTEL_RUN_B

    @pytest.fixture
    def intel_db(self, tmp_path):
        db_path = tmp_path / "intel.db"
        backend = SQLiteBackend(db_path=db_path)
        backend.initialize()
        load_intel_runs(backend)
        backend.close()
        return str(db_path)

    def _invoke(self, *args, columns=200, exit_code=0, stream="output", color=False):
        from unittest.mock import patch

        from rich.console import Console

        console = (
            Console(width=columns, force_terminal=True, color_system="standard")
            if color
            else Console(width=columns, color_system=None)
        )
        with patch("topdown.cli.console", console), patch("topdown.output.terminal.console", console):
            result = runner.invoke(app, list(args))
        assert result.exit_code == exit_code, result.output
        return getattr(result, stream)

    @pytest.fixture
    def spr_db(self, tmp_path):
        from tests.conftest import TEST_DATA_DIR
        from topdown.collector.csv_parser import parse_output

        db_path = tmp_path / "spr.db"
        backend = SQLiteBackend(db_path=db_path)
        backend.initialize()
        run = Run(run_id=self.RUN_A, process_name="redis-server", level=4, labels={"test_name": "spr"})
        backend.insert_run(run)
        backend.insert_samples([
            Sample(run_id=run.run_id, timestamp=x.timestamp, metric_name=x.metric_name, value=x.value, unit=x.unit)
            for x in parse_output((TEST_DATA_DIR / "toplev_csv_extended_spr.csv").read_text())
        ])
        backend.close()
        return str(db_path)


class TestCliIntelUnits(_IntelCli):
    """toplev-style units and full run IDs through the CLI renderers."""

    def test_list_prints_full_ids(self, intel_db):
        out = self._invoke("list", "--db", intel_db, "--last", "24h")
        assert self.RUN_A in out and self.RUN_B in out

    def test_funnel_labels_units_and_reports_mux(self, intel_db):
        out = self._invoke("query", "--db", intel_db, "--run-id", self.RUN_A, "--funnel", "--level", "3")
        assert self.RUN_A in out
        line = next(ln for ln in out.splitlines() if "Serializing_Operation" in ln)
        assert "61.0% of Clocks" in line and "█" not in line
        assert "Data quality: MUX 3.0%" in out

    def test_bottlenecks_keep_clock_nodes_out_of_the_ranking(self, intel_db):
        out = self._invoke("query", "--db", intel_db, "--run-id", self.RUN_A, "--bottlenecks")
        ranked, _, detail = out.partition("Detail nodes")
        assert "Serializing_Operation" not in ranked
        row = next(ln for ln in detail.splitlines() if "Serializing_Operation" in ln)
        assert "(parent Core_Bound: 30.0% of slots)" in row
        assert "61.0% of Clocks" in row and "█" not in row
        assert "Data quality: MUX 3.0%" in out

    @pytest.mark.parametrize("mode", [["--bottlenecks"], ["--funnel", "--level", "3"], ["--tree"], []])
    def test_node_names_survive_80_columns(self, intel_db, mode):
        out = self._invoke("query", "--db", intel_db, "--run-id", self.RUN_A, *mode, "--min-pct", "5", columns=80)
        for name in ("Core_Bound", "Serializing_Operation"):
            assert name in out, out
        assert "61.0% of Clocks" in out
        if mode == ["--bottlenecks"]:
            assert "L1 " in out and "L3 " in out, out  # the Level column isn't cropped
        assert max(len(ln) for ln in out.splitlines()) <= 80

    def test_by_bottleneck_fits_80_columns(self, intel_db):
        out = self._invoke("query", "--db", intel_db, "--bottleneck", "L1_Bound", "--last", "24h", columns=80)
        assert "L1_Bound" in out and "12.0% of Stalls" in out
        assert self.RUN_A in out

    def test_compare_fits_80_columns(self, intel_db):
        out = self._invoke("compare", self.RUN_A, self.RUN_B, "--db", intel_db, columns=80)
        assert self.RUN_A in out and self.RUN_B in out
        assert "Core_Bound" in out and "-10.0 pp" in out
        row = next(ln for ln in out.splitlines() if "Serializing" in ln)
        assert "of Clocks" in out and "-20.0 pp" in row and "changed" in row
        assert "Data quality: MUX 3.0%" in out

    def test_bottlenecks_json_is_clean_and_carries_details(self, intel_db):
        out = self._invoke("query", "--db", intel_db, "--run-id", self.RUN_A, "--bottlenecks", "--json", columns=80,
                           stream="stdout")
        rows = json.loads(out)
        detail = next(r for r in rows if r["metric_name"].endswith("Serializing_Operation"))
        assert (detail["kind"], detail["unit"]) == ("detail", "% Clocks")
        assert (detail["slot_ancestor"], detail["slot_ancestor_value"]) == ("Backend_Bound.Core_Bound", 30.0)
        assert {r["kind"] for r in rows} == {"slot", "detail"}

    @pytest.mark.parametrize("mode", [["--bottlenecks"], []])
    def test_csv_parses_at_80_columns(self, intel_db, mode):
        out = self._invoke("query", "--db", intel_db, "--run-id", self.RUN_A, *mode, "--csv", columns=80,
                           stream="stdout")
        rows = list(csv.DictReader(io.StringIO(out)))
        header = out.splitlines()[0].split(",")
        assert all(len(r) == len(header) for r in rows)
        detail = next(r for r in rows if r["metric_name"].endswith("Serializing_Operation"))
        assert (detail["kind"], detail["slot_ancestor"]) == ("detail", "Backend_Bound.Core_Bound")

    def test_default_view_json_carries_details(self, intel_db):
        rows = json.loads(self._invoke("query", "--db", intel_db, "--run-id", self.RUN_A, "--json", stream="stdout"))
        assert "detail" in {r["kind"] for r in rows}

    @pytest.mark.parametrize("mode", [["--bottlenecks", "--json"], ["--json"], ["--tree", "--json"],
                                      ["--bottlenecks", "--csv"]])
    def test_machine_output_puts_mux_on_stderr(self, intel_db, mode):
        err = self._invoke("query", "--db", intel_db, "--run-id", self.RUN_A, *mode, stream="stderr")
        assert err.startswith("Data quality: MUX 3.0%")

    @pytest.mark.parametrize("mode", [["--funnel", "--json"], ["--tree", "--json"]])
    def test_json_is_clean_at_80_columns(self, spr_db, mode):
        data = json.loads(self._invoke("query", "--db", spr_db, "--run-id", self.RUN_A, *mode, columns=80,
                                       stream="stdout"))
        assert data

    def test_compare_json_is_clean_and_mux_goes_to_stderr(self, intel_db):
        out = self._invoke("compare", self.RUN_A, self.RUN_B, "--db", intel_db, "--json", columns=80,
                           stream="stdout")
        assert {d["metric_name"] for d in json.loads(out)} >= {"Backend_Bound.Core_Bound"}
        err = self._invoke("compare", self.RUN_A, self.RUN_B, "--db", intel_db, "--json", stream="stderr")
        assert err.startswith("Data quality: MUX 3.0%")

    @pytest.mark.parametrize("level", ["3", "4"])
    def test_real_funnel_fits_80_columns(self, spr_db, level):
        out = self._invoke("query", "--db", spr_db, "--run-id", self.RUN_A, "--funnel", "--level", level, columns=80)
        lines = out.splitlines()
        assert max(len(ln) for ln in lines) <= 80
        names = ["Serializing_Operation", "Ports_Utilization"]
        if level == "4":
            names += ["Ports_Utilized_3m", "Ports_Utilized_1", "Ports_Utilized_2", "L1_Latency_Dependency"]
        for name in names:
            assert name in out, name

    def test_funnel_bars_are_proportional_at_80_columns(self, tmp_path):
        db_path = tmp_path / "bars.db"
        backend = SQLiteBackend(db_path=db_path)
        backend.initialize()
        run = Run(run_id=self.RUN_A, process_name="redis-server", level=2, labels={})
        backend.insert_run(run)
        backend.insert_samples([
            Sample(run_id=run.run_id, timestamp=1.0, metric_name="Backend_Bound", value=80.0, unit="%"),
            Sample(run_id=run.run_id, timestamp=1.0, metric_name="Backend_Bound.Memory_Bound", value=62.0, unit="%"),
        ])
        backend.close()
        out = self._invoke("query", "--db", str(db_path), "--run-id", self.RUN_A, "--funnel", columns=80)
        bars = {ln.split()[1]: ln.count("█") for ln in out.splitlines() if "█" in ln}
        assert bars["Backend_Bound"] > bars["Memory_Bound"] > 0
        # 100% = the whole Distribution column: its header cell's width.
        header = next(ln for ln in out.splitlines() if "Distribution" in ln)
        full = len(header.split("┃")[3]) - 2  # minus the cell padding
        assert abs(bars["Backend_Bound"] - 0.80 * full) <= 1 and abs(bars["Memory_Bound"] - 0.62 * full) <= 1
        assert "Useful work (Retiring): —" in out

    def test_list_shows_labels_literally(self, tmp_path):
        db_path = tmp_path / "markup.db"
        backend = SQLiteBackend(db_path=db_path)
        backend.initialize()
        backend.insert_run(Run(run_id=self.RUN_A, process_name="redis-server", level=1,
                               labels={"test_name": "t[bold]x"}))
        backend.close()
        for columns in (80, 200):
            assert "test_name=t[bold]x" in self._invoke("list", "--db", str(db_path), "--last", "24h",
                                                        columns=columns)

    def test_list_fits_80_columns(self, intel_db):
        out = self._invoke("list", "--db", intel_db, "--last", "24h", columns=80)
        assert self.RUN_A in out and "redis-server" in out and "test_name=intel-run" in out
        assert max(len(ln) for ln in out.splitlines()) <= 80

    @pytest.mark.parametrize("mode", [["--funnel"], ["--tree"]])
    def test_csv_is_rejected_where_unsupported(self, intel_db, mode):
        err = self._invoke("query", "--db", intel_db, "--run-id", self.RUN_A, *mode, "--csv", exit_code=2,
                           stream="stderr")
        assert "--csv is supported for" in err

    def test_default_view_covers_each_branch_with_details(self, intel_db):
        out = self._invoke("query", "--db", intel_db, "--run-id", self.RUN_A, "--min-pct", "5")
        deepest, _, detail = out.partition("Detail nodes")
        assert "Core_Bound" in deepest and "Memory_Bound" in deepest
        assert "Serializing_Operation" in detail
        assert "Data quality: MUX 3.0%" in out

    def test_tree_labels_units_and_reports_mux(self, intel_db):
        out = self._invoke("query", "--db", intel_db, "--run-id", self.RUN_A, "--tree")
        assert "61.0% of Clocks" in out
        assert "Data quality: MUX 3.0%" in out

    def test_tree_json_units_have_no_marker(self, intel_db):
        out = self._invoke("query", "--db", intel_db, "--run-id", self.RUN_A, "--tree", "--json")
        assert '"% Slots <"' not in out and '"% Slots"' in out

    def test_by_bottleneck_fallback_is_announced(self, intel_db):
        out = self._invoke("query", "--db", intel_db, "--bottleneck", "Serializing", "--min-pct", "5", "--last", "24h")
        assert "No node is named exactly 'Serializing'" in out
        assert "61.0% of Clocks" in out and self.RUN_A in out
        rows = json.loads(self._invoke("query", "--db", intel_db, "--bottleneck", "Serializing", "--min-pct", "5",
                                       "--last", "24h", "--json", stream="stdout"))
        assert rows[0]["metric_name"].endswith("Serializing_Operation")
        assert (rows[0]["match"], rows[0]["truncated"]) == ("substring", False)

    def test_by_bottleneck_empty_messages(self, intel_db):
        out = self._invoke("query", "--db", intel_db, "--bottleneck", "Core_Bound", "--min-pct", "35", "--last", "24h")
        assert "No runs found where Core_Bound >= 35.0%." in out and "Serializing" not in out
        out = self._invoke("query", "--db", intel_db, "--bottleneck", "Nonexistent", "--last", "24h")
        assert "No node named or containing 'Nonexistent' reaches 5.0%." in out
        out = self._invoke("query", "--db", intel_db, "--bottleneck", "MUX", "--last", "24h")
        assert "MUX is a data-quality metric, not a TMA node" in out

    def test_by_bottleneck_says_when_results_are_capped(self, tmp_path):
        from topdown.storage.base import DEFAULT_NODE_LIMIT

        db_path = tmp_path / "many.db"
        backend = SQLiteBackend(db_path=db_path)
        backend.initialize()
        for _ in range(DEFAULT_NODE_LIMIT + 1):
            run = Run(process_name="redis-server", level=1, labels={})
            backend.insert_run(run)
            backend.insert_samples([Sample(run_id=run.run_id, timestamp=1.0, metric_name="Backend_Bound",
                                           value=40.0, unit="%")])
        backend.close()
        out = self._invoke("query", "--db", str(db_path), "--bottleneck", "Backend_Bound", "--last", "24h")
        assert f"Showing the first {DEFAULT_NODE_LIMIT}" in out

    def test_explain_mux(self):
        assert "event-multiplexing" in self._invoke("explain", "MUX")
        assert json.loads(self._invoke("explain", "MUX", "--json", stream="stdout"))["kind"] == "data-quality"


class TestCliNarrowAndMachineOutput(_IntelCli):
    """80-column, colour and stderr details, on the shared Intel fixture."""

    @pytest.mark.parametrize("args", [["--funnel", "--json"], ["--tree", "--json"], ["--bottlenecks", "--json"]])
    def test_json_has_no_ansi_even_with_forced_colour(self, intel_db, args):
        out = self._invoke("query", "--db", intel_db, "--run-id", self.RUN_A, *args, stream="stdout", color=True)
        assert "\x1b[" not in out
        json.loads(out)

    def test_compare_json_has_no_ansi_even_with_forced_colour(self, intel_db):
        out = self._invoke("compare", self.RUN_A, self.RUN_B, "--db", intel_db, "--json", stream="stdout", color=True)
        assert "\x1b[" not in out
        json.loads(out)

    def test_narrow_funnel_drops_bars_but_keeps_values(self, spr_db):
        out = self._invoke("query", "--db", spr_db, "--run-id", self.RUN_A, "--funnel", "--level", "4", columns=50)
        # Names longer than the room left fold onto a second line; values never do.
        assert "Distribution" not in out and "55.4%" in out and "61.1% of Clocks" in out
        assert max(len(ln) for ln in out.splitlines()) <= 50

    def test_long_funnel_names_fold_instead_of_truncating(self, tmp_path):
        name = "Backend_Bound.Memory_Bound." + "Extremely_Long_Node_Name_For_Testing_" * 2
        db_path = tmp_path / "long.db"
        backend = SQLiteBackend(db_path=db_path)
        backend.initialize()
        backend.insert_run(Run(run_id=self.RUN_A, process_name="redis-server", level=3, labels={}))
        backend.insert_samples([Sample(run_id=self.RUN_A, timestamp=1.0, metric_name=n, value=v, unit="%")
                                for n, v in (("Backend_Bound", 50.0), ("Backend_Bound.Memory_Bound", 40.0),
                                             (name, 30.0))])
        backend.close()
        out = self._invoke("query", "--db", str(db_path), "--run-id", self.RUN_A, "--funnel", columns=80)
        assert "…" not in out
        # The Metric column's text, read top to bottom, holds the whole name.
        metric_cells = "".join(ln.split("│")[1].strip() for ln in out.splitlines() if ln.startswith("│"))
        assert metric_cells.count("Extremely_Long_Node_Name_For_Testing_") == 2

    def test_share_column_is_never_cut(self, spr_db):
        out = self._invoke("query", "--db", spr_db, "--run-id", self.RUN_A, "--funnel", "--level", "6", columns=45)
        assert "55.4%" in out and "61.1% of Clocks" in out

    def test_bars_dropped_when_too_narrow(self, tmp_path):
        db_path = tmp_path / "narrow.db"
        backend = SQLiteBackend(db_path=db_path)
        backend.initialize()
        backend.insert_run(Run(run_id=self.RUN_A, process_name="redis-server", level=1, labels={}))
        backend.insert_samples([
            Sample(run_id=self.RUN_A, timestamp=1.0, metric_name="Frontend_Bound", value=60.0, unit="%"),
            Sample(run_id=self.RUN_A, timestamp=1.0, metric_name="Retiring", value=40.0, unit="%"),
        ])
        backend.close()
        # 40 columns leave 7 cells for bars here: below MIN_BAR_WIDTH, so no bar column.
        out = self._invoke("query", "--db", str(db_path), "--run-id", self.RUN_A, "--funnel", columns=40)
        assert "█" not in out and "Frontend_Bound" in out and "60.0%" in out
        out = self._invoke("query", "--db", str(db_path), "--run-id", self.RUN_A, "--funnel", columns=60)
        assert "Distribution" in out and "█" in out

    def test_real_bottleneck_table_keeps_names_at_80_columns(self, spr_db):
        for mode in (["--bottlenecks", "--min-pct", "2", "--top-n", "30"], ["--min-pct", "2", "--top-n", "30"]):
            out = self._invoke("query", "--db", spr_db, "--run-id", self.RUN_A, *mode, columns=80)
            for name in ("Branch_Mispredicts", "Microcode_Sequencer", "Serializing_Operation"):
                assert name in out, (mode, name, out)
            assert max(len(ln) for ln in out.splitlines()) <= 80

    def test_compare_without_differences_prints_mux(self, intel_db):
        out = self._invoke("compare", self.RUN_A, self.RUN_A, "--db", intel_db)
        assert "No significant differences found." in out and "Data quality: MUX 3.0%" in out

    def test_compare_table_has_composition_note(self, intel_db):
        out = self._invoke("compare", self.RUN_A, self.RUN_B, "--db", intel_db)
        assert "Slot shares add up to 100%" in out

    def test_node_query_labels_are_literal(self, tmp_path):
        db_path = tmp_path / "markup.db"
        backend = SQLiteBackend(db_path=db_path)
        backend.initialize()
        backend.insert_run(Run(run_id=self.RUN_A, process_name="redis-server", level=1,
                               labels={"test_name": "t[bold]x"}))
        backend.insert_samples([Sample(run_id=self.RUN_A, timestamp=1.0, metric_name="Backend_Bound", value=40.0,
                                       unit="%")])
        backend.close()
        out = self._invoke("query", "--db", str(db_path), "--bottleneck", "Backend_Bound", "--last", "24h")
        assert "test_name=t[bold]x" in out

    def test_machine_mode_messages_go_to_stderr(self, intel_db):
        for name, message in (("MUX", "data-quality metric"), ("Nope", "No node named or containing 'Nope'")):
            args = ("query", "--db", intel_db, "--bottleneck", name, "--last", "24h", "--json")
            assert json.loads(self._invoke(*args, stream="stdout")) == []
            assert message in self._invoke(*args, stream="stderr")
        assert "Unknown metric 'Nope'" in self._invoke("explain", "Nope", "--json", exit_code=1, stream="stderr")
