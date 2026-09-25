"""Unit handling for real toplev output: " <" markers, non-slot units, MUX.

The fixture is one interval of `toplev -l4 -I1000 -x, --no-desc -v` captured on
a Sapphire Rapids (Xeon 8488C) host. Before these fixes, Intel runs stored
units like "% Slots <", which made query_bottlenecks/query_by_bottleneck ignore
every Intel metric and split a metric into two partial averages whenever it
crossed toplev's threshold mid-run.
"""

import pytest

from tests.conftest import TEST_DATA_DIR
from topdown.analysis.bottleneck import (
    find_bottlenecks,
    find_deepest_bottlenecks,
    find_detail_bottlenecks,
    summarize_bottlenecks,
)
from topdown.analysis.compare import compare_runs, summarize_comparison
from topdown.analysis.funnel import build_funnel, format_funnel_comparison, format_funnel_text
from topdown.analysis.topdown_tree import build_tree, format_tree_text
from topdown.collector.csv_parser import CsvFormat, parse_line, parse_output
from topdown.units import (
    COMPOSITION_NOTE,
    delta_direction,
    format_value,
    is_below_threshold,
    is_bottleneck_candidate,
    is_percent_unit,
    is_slot_unit,
    normalize_unit,
    normalized_unit_sql,
    percent_unit_sql,
    quality_metrics_sql,
    slot_unit_sql,
    unit_class,
    unit_suffix,
)


@pytest.fixture(scope="module")
def spr_samples():
    return parse_output((TEST_DATA_DIR / "toplev_csv_extended_spr.csv").read_text())


@pytest.fixture(scope="module")
def spr_metrics(spr_samples):
    return [{"metric_name": s.metric_name, "value": s.value, "unit": s.unit} for s in spr_samples]


# Slot-only data as the AMD (uProf) and ARM (perf stat) collectors store it.
AMD_METRICS = [
    {"metric_name": "Frontend_Bound", "value": 60.0, "unit": "%"},
    {"metric_name": "Frontend_Bound.Fetch_Latency", "value": 35.0, "unit": "%"},
    {"metric_name": "Retiring", "value": 40.0, "unit": "%"},
]


class TestUnitHelpers:
    def test_normalize_strips_below_threshold_marker(self):
        assert normalize_unit("% Slots <") == "% Slots"
        assert normalize_unit("% Clocks_est <") == "% Clocks_est"
        assert normalize_unit("% Slots") == "% Slots"
        assert normalize_unit("%") == "%"
        assert normalize_unit("") == "%"
        assert normalize_unit(None) == "%"

    def test_below_threshold(self):
        assert is_below_threshold("% Slots <")
        assert not is_below_threshold("% Slots")
        assert not is_below_threshold("%")

    def test_slot_units(self):
        for u in ("%", "% Slots <", "% Slots_est", "% Slots_calc", "%_Slots", "slots"):
            assert is_slot_unit(u), u
        for u in ("% Clocks", "% Stalls <", "% Uops", "Metric"):
            assert not is_slot_unit(u), u

    def test_percent_units(self):
        for u in ("%", "% Slots", "% Clocks <", "% Stalls", "% Uops <", "%_Slots", "slots"):
            assert is_percent_unit(u), u
        assert not is_percent_unit("Metric")
        assert not is_percent_unit("GHz")

    def test_unit_suffix(self):
        assert unit_suffix("% Slots <") == ""
        assert unit_suffix("%") == ""
        assert unit_suffix("% Clocks <") == " of Clocks"
        assert unit_suffix("% Stalls") == " of Stalls"
        assert unit_suffix("Metric") == ""

    def test_format_value(self):
        assert format_value(40.44, "% Slots <") == "40.4%"
        assert format_value(28.2, "% Stalls") == "28.2% of Stalls"
        assert format_value(2.5, "Metric") == "2.50 Metric"

    def test_sql_fragments_agree_with_python(self):
        import sqlite3

        conn = sqlite3.connect(":memory:")
        for unit in (
            "% Slots <",
            "% Slots",
            "% Slots_est <",
            "%",
            "% Clocks <",
            "% Stalls",
            "%_Slots",
            "slots",
            "<",
            "Metric",
            "",
        ):
            row = conn.execute(
                f"SELECT {normalized_unit_sql('u')}, {slot_unit_sql(normalized_unit_sql('u'))}, "
                f"{percent_unit_sql('u')} FROM (SELECT ? AS u)",
                (unit,),
            ).fetchone()
            assert row[0] == normalize_unit(unit), unit
            assert bool(row[1]) == is_slot_unit(unit), unit
            if unit:
                assert bool(row[2]) == (unit.startswith("%") or unit == "slots"), unit
        assert quality_metrics_sql("s.metric_name") == "upper(s.metric_name) NOT IN ('MUX')"
        from topdown.units import bottleneck_candidate_sql

        for name in (
            "",
            "mux",
            "MUX",
            "Retiring",
            "Retiring.Light_Operations",
            "Retiring.Light_Operations.X",
            "Retiring.Light_OperationsX",
            "Retiring.Heavy_Operations",
            "Backend_Bound",
        ):
            got = conn.execute(f"SELECT {bottleneck_candidate_sql('n')} FROM (SELECT ? AS n)", (name,)).fetchone()[0]
            assert bool(got) == is_bottleneck_candidate(name), name

    def test_unit_class(self):
        assert unit_class("%") == unit_class("% Slots <") == unit_class("%_Slots") == "slots"
        assert unit_class("% Clocks <") == "% Clocks"

    def test_one_useful_work_rule_everywhere(self):
        from topdown.units import is_useful_work

        for name in ("Retiring", "Retiring.Light_Operations", "Retiring.Light_Operations.FP_Arith"):
            assert is_useful_work(name) and not is_bottleneck_candidate(name), name
        heavy = "Retiring.Heavy_Operations.Microcode_Sequencer"
        assert not is_useful_work(heavy) and is_bottleneck_candidate(heavy)
        assert delta_direction(heavy, "%") == -1
        entries = {
            e.metric_name: e
            for e in build_funnel(
                [
                    {"metric_name": "Retiring", "value": 30.0, "unit": "%"},
                    {"metric_name": "Retiring.Heavy_Operations", "value": 10.0, "unit": "%"},
                    {"metric_name": heavy, "value": 8.0, "unit": "%"},
                ],
                max_level=3,
            ).entries
        }
        assert entries["Retiring"].is_useful and not entries[heavy].is_useful

    def test_delta_arrow_and_format(self):
        from topdown.units import delta_arrow, format_delta

        assert [delta_arrow(d) for d in (2.0, -2.0, 0.0)] == ["↑", "↓", "="]
        assert format_delta(2.0, "% Clocks") == "+2.0 pp"
        assert format_delta(0.25, "Metric") == "+0.25"

    def test_bottleneck_table_draws_bars_only_for_slot_rows(self):
        from rich.console import Console

        from topdown.analysis.bottleneck import Bottleneck
        from topdown.output import terminal

        rows = [
            Bottleneck("Backend_Bound", "Backend_Bound", 40.0, "% Slots", 1),
            Bottleneck("Backend_Bound.Core_Bound.Divider", "Divider", 90.0, "% Clocks", 3),
        ]
        console = Console(width=120, color_system=None, record=True)
        original, terminal.console = terminal.console, console
        try:
            terminal.print_bottlenecks(rows)
        finally:
            terminal.console = original
        lines = console.export_text().splitlines()
        assert "█" in next(ln for ln in lines if "Backend_Bound" in ln)
        assert "█" not in next(ln for ln in lines if "Divider" in ln)

    def test_non_slot_values_use_a_neutral_color(self):
        from topdown.output.terminal import NEUTRAL, color_for_value, value_style

        assert value_style(61.0, "% Clocks") == NEUTRAL
        assert value_style(61.0, "% Slots") == color_for_value(61.0) != NEUTRAL

    def test_mux_caveat_states_the_scale(self):
        from topdown.units import mux_caveat

        assert "(~33x)" in mux_caveat(3.0)
        assert "only ~0.4% of an interval" in mux_caveat(0.4)
        assert mux_caveat(100.0) == ""

    def test_quality_metrics_ignore_case(self):
        from topdown.units import is_quality_metric

        assert is_quality_metric("mux") and is_quality_metric("MUX")
        assert not is_bottleneck_candidate("mux")

    def test_estimated_slot_shares_are_marked(self):
        assert unit_suffix("% Slots_est <") == " (est.)"
        assert format_value(7.5, "% Slots_est") == "7.5% (est.)"
        assert unit_suffix("% Slots") == ""

    def test_bottleneck_candidates(self):
        for name in ("Retiring", "Retiring.Light_Operations", "Retiring.Light_Operations.FP_Arith", "MUX", ""):
            assert not is_bottleneck_candidate(name), name
        for name in ("Backend_Bound", "Retiring.Heavy_Operations.Microcode_Sequencer"):
            assert is_bottleneck_candidate(name), name

    def test_delta_direction(self):
        assert delta_direction("Retiring", "% Slots") == 1
        assert delta_direction("Backend_Bound.Memory_Bound", "%") == -1
        assert delta_direction("Retiring.Light_Operations.Memory_Operations", "% Slots") == 0
        assert delta_direction("Retiring.Heavy_Operations.Microcode_Sequencer", "% Slots") == -1
        assert delta_direction("Backend_Bound.Core_Bound.Ports_Utilization", "% Clocks") == 0
        assert delta_direction("MUX", "%") == 0


class TestParserOnRealToplev:
    def test_no_stored_unit_carries_the_marker(self, spr_samples):
        assert len(spr_samples) > 80
        assert not [s for s in spr_samples if s.unit.endswith("<")]

    def test_marker_moves_to_status(self, spr_samples):
        by_name = {s.metric_name: s for s in spr_samples}
        dsb = by_name["Frontend_Bound.Fetch_Bandwidth.DSB"]  # "% Slots_est <" in the CSV
        assert (dsb.unit, dsb.status) == ("% Slots_est", "<")
        fe = by_name["Frontend_Bound"]  # "% Slots", above threshold
        assert (fe.unit, fe.status) == ("% Slots", "")

    def test_bottleneck_marker_wins_over_threshold_marker(self):
        line = "1.0,Frontend_Bound,20.7,% Slots <,,x,0.0,3.0,<==,"
        s = parse_line(line, CsvFormat.EXTENDED_10COL)
        assert (s.unit, s.status) == ("% Slots", "<==")

    def test_mux_is_parsed(self, spr_samples):
        assert {s.metric_name: s.value for s in spr_samples}["MUX"] == 3.0


class TestBottleneckRanking:
    def test_only_slot_shares_are_ranked(self, spr_metrics):
        found = find_bottlenecks(spr_metrics, top_n=50, min_percentage=0.0)
        assert found
        assert all(is_slot_unit(b.unit) for b in found)
        names = [b.metric_name for b in found]
        # 61.1% of Clocks under a 30.1%-of-slots parent must not outrank slot shares.
        assert "Backend_Bound.Core_Bound.Serializing_Operation" not in names
        assert names[:3] == ["Backend_Bound", "Backend_Bound.Core_Bound", "Backend_Bound.Memory_Bound"]

    def test_useful_work_and_mux_are_not_bottlenecks(self, spr_metrics):
        names = [b.metric_name for b in find_bottlenecks(spr_metrics, top_n=50, min_percentage=0.0)]
        assert "Retiring" not in names
        assert not [n for n in names if n.startswith("Retiring.Light_Operations")]
        assert "Retiring.Heavy_Operations.Microcode_Sequencer" in names
        assert "MUX" not in names
        with_useful = find_bottlenecks(spr_metrics, top_n=50, min_percentage=0.0, include_useful=True)
        assert "Retiring" in [b.metric_name for b in with_useful]

    def test_mux_never_a_bottleneck_even_when_large(self):
        metrics = [{"metric_name": "MUX", "value": 100.0, "unit": "%"}]
        assert find_bottlenecks(metrics) == []
        assert find_deepest_bottlenecks(metrics) == []
        assert find_detail_bottlenecks(metrics) == []

    def test_legacy_marked_units_still_count(self):
        # Runs stored before the parser fix keep "% Slots <" in the database.
        metrics = [{"metric_name": "Backend_Bound", "value": 40.0, "unit": "% Slots <"}]
        assert [b.metric_name for b in find_bottlenecks(metrics)] == ["Backend_Bound"]

    def test_details_are_ordered_by_their_slot_parent(self):
        metrics = [
            {"metric_name": "Backend_Bound", "value": 50.0, "unit": "% Slots"},
            {"metric_name": "Backend_Bound.Core_Bound", "value": 5.0, "unit": "% Slots"},
            {"metric_name": "Backend_Bound.Core_Bound.Ports_Utilization", "value": 70.0, "unit": "% Clocks"},
            {"metric_name": "Backend_Bound.Memory_Bound", "value": 40.0, "unit": "% Slots"},
            {"metric_name": "Backend_Bound.Memory_Bound.L3_Bound", "value": 20.0, "unit": "% Stalls <"},
        ]
        details = find_detail_bottlenecks(metrics)
        assert [(d.metric_name.split(".")[-1], d.slot_ancestor.split(".")[-1]) for d in details] == [
            ("L3_Bound", "Memory_Bound"),
            ("Ports_Utilization", "Core_Bound"),
        ]
        assert details[0].to_dict()["slot_ancestor_value"] == 40.0
        assert details[0].to_dict()["kind"] == "detail"

    def test_heavy_operations_costs_can_be_bottlenecks(self):
        metrics = [
            {"metric_name": "Retiring", "value": 30.0, "unit": "%"},
            {"metric_name": "Retiring.Light_Operations", "value": 20.0, "unit": "%"},
            {"metric_name": "Retiring.Heavy_Operations", "value": 10.0, "unit": "%"},
            {"metric_name": "Retiring.Heavy_Operations.Microcode_Sequencer", "value": 8.0, "unit": "%"},
        ]
        assert [b.metric_name for b in find_bottlenecks(metrics)] == [
            "Retiring.Heavy_Operations",
            "Retiring.Heavy_Operations.Microcode_Sequencer",
        ]

    def test_deepest_covers_each_level2_branch(self, spr_metrics):
        deepest = find_deepest_bottlenecks(spr_metrics, min_percentage=5.0)
        assert [b.metric_name for b in deepest] == [
            "Backend_Bound.Core_Bound",
            "Backend_Bound.Memory_Bound",
            "Frontend_Bound.Fetch_Latency",
            "Frontend_Bound.Fetch_Bandwidth.MS",
            "Bad_Speculation.Branch_Mispredicts",
        ]

    @pytest.mark.parametrize("big, small", [("MS", "MITE"), ("A_Big", "Z_Small")])
    def test_deepest_follows_the_largest_child(self, big, small):
        # Names in both orders, so neither depth nor name can stand in for size.
        metrics = [
            {"metric_name": "Frontend_Bound", "value": 50.0, "unit": "%"},
            {"metric_name": "Frontend_Bound.Fetch_Bandwidth", "value": 44.0, "unit": "%"},
            {"metric_name": f"Frontend_Bound.Fetch_Bandwidth.{big}", "value": 38.0, "unit": "%"},
            {"metric_name": f"Frontend_Bound.Fetch_Bandwidth.{small}", "value": 6.0, "unit": "%"},
            {"metric_name": f"Frontend_Bound.Fetch_Bandwidth.{small}.Decoder0_Alone", "value": 5.2, "unit": "%"},
        ]
        assert [b.metric_name for b in find_deepest_bottlenecks(metrics)] == [f"Frontend_Bound.Fetch_Bandwidth.{big}"]

    def test_deepest_breaks_child_ties_by_name(self):
        metrics = [
            {"metric_name": n, "value": 10.0, "unit": "%"}
            for n in (
                "Backend_Bound",
                "Backend_Bound.Core_Bound",
                "Backend_Bound.Core_Bound.Z_Node",
                "Backend_Bound.Core_Bound.A_Node",
            )
        ]
        assert [b.metric_name for b in find_deepest_bottlenecks(metrics)] == ["Backend_Bound.Core_Bound.A_Node"]

    def test_deepest_respects_top_n(self, spr_metrics):
        assert len(find_deepest_bottlenecks(spr_metrics, top_n=2)) == 2

    def test_deepest_reports_a_root_only_without_branches(self):
        metrics = [
            {"metric_name": "Frontend_Bound", "value": 20.0, "unit": "%"},
            {"metric_name": "Frontend_Bound.Fetch_Latency", "value": 2.0, "unit": "%"},
            {"metric_name": "Backend_Bound", "value": 30.0, "unit": "%"},
            {"metric_name": "Backend_Bound.Memory_Bound", "value": 25.0, "unit": "%"},
        ]
        assert [b.metric_name for b in find_deepest_bottlenecks(metrics)] == [
            "Backend_Bound.Memory_Bound",
            "Frontend_Bound",
        ]

    def test_details_only_hang_off_slot_share_parents(self, spr_metrics):
        names = [d.metric_name.split(".")[-1] for d in find_detail_bottlenecks(spr_metrics, top_n=50)]
        # MEM_Latency's parent is DRAM_Bound (0.0% of Stalls); Ports_Utilized_* sit
        # under Ports_Utilization (% Clocks). Neither parent is a slot share.
        assert "MEM_Latency" not in names
        assert not [n for n in names if n.startswith("Ports_Utilized")]
        assert names == ["Serializing_Operation", "Ports_Utilization", "L1_Bound", "L3_Bound", "MS_Switches"]

    def test_details_capped_per_parent_and_by_level(self):
        metrics = [
            {"metric_name": "Backend_Bound", "value": 50.0, "unit": "%"},
            {"metric_name": "Backend_Bound.Core_Bound", "value": 30.0, "unit": "%"},
            *[
                {"metric_name": f"Backend_Bound.Core_Bound.N{i}", "value": 10.0 + i, "unit": "% Clocks"}
                for i in range(4)
            ],
        ]
        details = find_detail_bottlenecks(metrics, top_n=10)
        assert [d.metric_name.split(".")[-1] for d in details] == ["N3", "N2"]
        assert find_detail_bottlenecks(metrics, top_n=10, max_level=2) == []

    def test_summary_labels_detail_nodes_and_mux(self, spr_metrics):
        text = summarize_bottlenecks(
            find_bottlenecks(spr_metrics, top_n=2), find_detail_bottlenecks(spr_metrics, top_n=1), mux=3.0
        )
        assert text.splitlines()[:6] == [
            "Top bottlenecks:",
            "  Backend_Bound: 55.4%",
            "  Backend_Bound -> Core_Bound: 30.1%",
            "",
            "Detail nodes (up to 2 per parent; fraction of their own denominator, not of pipeline slots):",
            (
                "  Backend_Bound -> Core_Bound -> Serializing_Operation: 61.1% of Clocks"
                "  (parent Core_Bound: 30.1% of slots)"
            ),
        ]
        assert text.splitlines()[-1].startswith("Data quality: MUX 3.0%")

    def test_summary_without_ranked_nodes(self):
        details = find_detail_bottlenecks(
            [
                {"metric_name": "Backend_Bound.Core_Bound", "value": 30.0, "unit": "%"},
                {"metric_name": "Backend_Bound.Core_Bound.Divider", "value": 50.0, "unit": "% Clocks"},
            ]
        )
        text = summarize_bottlenecks([], details, mux=3.0)
        assert text.splitlines()[0].startswith("Detail nodes")
        assert "Top bottlenecks" not in text
        empty = summarize_bottlenecks([], [], mux=3.0).splitlines()
        assert empty[0] == "No significant bottlenecks detected."
        assert empty[-1].startswith("Data quality: MUX 3.0%")

    def test_details_need_a_parent_above_threshold(self):
        metrics = [
            {"metric_name": "Backend_Bound.Core_Bound", "value": 0.5, "unit": "%"},
            {"metric_name": "Backend_Bound.Core_Bound.Serializing_Operation", "value": 61.0, "unit": "% Clocks"},
        ]
        assert find_detail_bottlenecks(metrics, min_percentage=5.0) == []

    def test_details_exclude_useful_work_and_respect_top_n(self):
        metrics = [
            {"metric_name": "Retiring.Light_Operations", "value": 30.0, "unit": "%"},
            {"metric_name": "Retiring.Light_Operations.FP_Arith", "value": 40.0, "unit": "% Uops"},
            {"metric_name": "Backend_Bound.Core_Bound", "value": 30.0, "unit": "%"},
            {"metric_name": "Backend_Bound.Core_Bound.Divider", "value": 20.0, "unit": "% Clocks"},
            {"metric_name": "Backend_Bound.Memory_Bound", "value": 20.0, "unit": "%"},
            {"metric_name": "Backend_Bound.Memory_Bound.L3_Bound", "value": 20.0, "unit": "% Stalls"},
        ]
        assert [d.metric_name.split(".")[-1] for d in find_detail_bottlenecks(metrics)] == ["Divider", "L3_Bound"]
        assert len(find_detail_bottlenecks(metrics, top_n=1)) == 1


class TestFunnelUnits:
    def test_mux_is_quality_not_a_node(self, spr_metrics):
        result = build_funnel(spr_metrics, max_level=4)
        assert result.mux == 3.0
        assert "MUX" not in [e.metric_name for e in result.entries]
        assert format_funnel_text(result).splitlines()[-1] == (
            "Data quality: MUX 3.0% (lowest per-interval event-multiplexing ratio). "
            "Some event groups were counted for only ~3% of an interval and their values extrapolated "
            "(~33x), at any level; treat small differences as noise."
        )

    def test_mux_uses_the_lowest_interval(self):
        aggregated = [{"metric_name": "MUX", "avg_value": 40.0, "min_value": 2.0, "unit": "%"}]
        assert build_funnel(aggregated).mux == 2.0
        raw = [{"metric_name": "MUX", "value": v, "unit": "%"} for v in (9.0, 3.0, 6.0)]
        assert build_funnel(raw).mux == 3.0

    def test_non_slot_rows_are_labelled_without_bars(self, spr_metrics):
        lines = format_funnel_text(build_funnel(spr_metrics, max_level=3)).splitlines()
        ports = next(ln for ln in lines if "Ports_Utilization" in ln)
        assert ports.split() == ["Ports_Utilization", "20.5%", "of", "Clocks", "✗"]
        core = next(ln for ln in lines if ln.strip().startswith("Core_Bound"))
        assert "█" in core
        assert any(ln.startswith("Rows marked 'of Clocks'") for ln in lines)

    def test_slot_children_listed_before_other_units(self):
        metrics = [
            {"metric_name": "Backend_Bound", "value": 50.0, "unit": "% Slots"},
            {"metric_name": "Backend_Bound.Core_Bound", "value": 30.0, "unit": "% Slots"},
            {"metric_name": "Backend_Bound.Core_Bound.Divider", "value": 90.0, "unit": "% Clocks"},
            {"metric_name": "Backend_Bound.Core_Bound.Slot_Child", "value": 5.0, "unit": "% Slots"},
        ]
        entries = [e.path for e in build_funnel(metrics, max_level=3).entries]
        assert entries == ["Backend_Bound", "Core_Bound", "Slot_Child", "Divider"]
        tree = format_tree_text(build_tree(metrics)).splitlines()
        assert [ln.split()[0] for ln in tree] == ["Backend_Bound", "Core_Bound", "Slot_Child", "Divider"]

    def test_non_percent_rows_stay_out_of_the_funnel(self):
        metrics = [*AMD_METRICS, {"metric_name": "IPC", "value": 2.5, "unit": "Metric"}]
        assert "IPC" not in [e.metric_name for e in build_funnel(metrics).entries]

    def test_slot_only_output_is_unchanged(self):
        # Byte-for-byte what the funnel printed before unit handling existed.
        assert format_funnel_text(build_funnel(AMD_METRICS)) == (
            "Pipeline Slots Funnel (100% total)\n"
            "  Useful work (Retiring): 40.0%\n"
            "  Wasted:                 60.0%\n"
            "\n"
            "  Frontend_Bound                        60.0%  ██████████████████████████████ ✗\n"
            "    Fetch_Latency                         35.0%  █████████████████ ✗\n"
            "  Retiring                              40.0%  ████████████████████ ✓"
        )


class TestFunnelComparison:
    def _pair(self, a, b):
        return format_funnel_comparison(build_funnel(a, 3), build_funnel(b, 3)).splitlines()

    def test_non_slot_rows_labelled_and_never_marked(self):
        a = [
            {"metric_name": "Backend_Bound", "value": 40.0, "unit": "% Slots"},
            {"metric_name": "Backend_Bound.Core_Bound", "value": 10.0, "unit": "% Slots"},
            {"metric_name": "Backend_Bound.Core_Bound.Ports_Utilization", "value": 20.0, "unit": "% Clocks"},
        ]
        b = [dict(m, value=m["value"] + 10.0) for m in a]
        lines = self._pair(a, b)
        ports = next(ln for ln in lines if "Ports_Utilization" in ln)
        assert ports.endswith("+10.0 pp of Clocks")
        backend = next(ln for ln in lines if ln.startswith("Backend_Bound"))
        assert backend.endswith("+10.0 pp ▼")

    def test_useful_work_mix_is_not_marked_but_heavy_ops_are(self):
        a = [
            {"metric_name": "Retiring", "value": 40.0, "unit": "%"},
            {"metric_name": "Retiring.Light_Operations", "value": 35.0, "unit": "%"},
            {"metric_name": "Retiring.Heavy_Operations", "value": 5.0, "unit": "%"},
        ]
        b = [
            {"metric_name": "Retiring", "value": 40.0, "unit": "%"},
            {"metric_name": "Retiring.Light_Operations", "value": 25.0, "unit": "%"},
            {"metric_name": "Retiring.Heavy_Operations", "value": 15.0, "unit": "%"},
        ]
        lines = self._pair(a, b)
        assert next(ln for ln in lines if "Light_Operations" in ln).endswith("-10.0 pp")
        assert next(ln for ln in lines if "Heavy_Operations" in ln).endswith("+10.0 pp ▼")

    def test_useful_work_row_needs_both_sides(self):
        a = [{"metric_name": "Backend_Bound", "value": 40.0, "unit": "%"}]
        b = [
            {"metric_name": "Backend_Bound", "value": 40.0, "unit": "%"},
            {"metric_name": "Retiring", "value": 20.0, "unit": "%"},
        ]
        summary = next(ln for ln in self._pair(a, b) if ln.startswith("Useful work"))
        assert summary.split()[-3:] == ["—", "20.0%", "—"]
        header = format_funnel_text(build_funnel(a)).splitlines()[1:3]
        assert header == ["  Useful work (Retiring): —", "  Wasted:                 —"]

    def test_b_only_sibling_keeps_its_own_children(self):
        a = [
            {"metric_name": "Frontend_Bound", "value": 30.0, "unit": "%"},
            {"metric_name": "Frontend_Bound.Fetch_Latency", "value": 20.0, "unit": "%"},
            {"metric_name": "Frontend_Bound.Fetch_Latency.MS_Switches", "value": 14.0, "unit": "% Clocks"},
            {"metric_name": "Frontend_Bound.Fetch_Latency.ITLB_Misses", "value": 2.0, "unit": "% Clocks"},
        ]
        b = [*a, {"metric_name": "Frontend_Bound.Fetch_Bandwidth", "value": 9.0, "unit": "%"}]
        rows = [next(t for t in ln.split() if t not in ("│", "├──", "└──")) for ln in self._pair(a, b)[2:7]]
        assert rows == ["Frontend_Bound", "Fetch_Latency", "MS_Switches", "ITLB_Misses", "Fetch_Bandwidth"]

    def test_b_only_slot_row_goes_before_other_units(self):
        a = [
            {"metric_name": "Backend_Bound", "value": 40.0, "unit": "%"},
            {"metric_name": "Backend_Bound.Core_Bound", "value": 20.0, "unit": "%"},
            {"metric_name": "Backend_Bound.Core_Bound.Divider", "value": 50.0, "unit": "% Clocks"},
        ]
        b = [*a, {"metric_name": "Backend_Bound.Core_Bound.Slot_Child", "value": 5.0, "unit": "%"}]
        names = [ln.split()[-5] if "── " in ln else "" for ln in self._pair(a, b)]
        rows = [ln for ln in self._pair(a, b) if "Slot_Child" in ln or "Divider" in ln]
        assert "Slot_Child" in rows[0] and "Divider" in rows[1], names

    def test_one_sided_and_mismatched_rows(self):
        a = [
            {"metric_name": "Backend_Bound", "value": 40.0, "unit": "% Slots"},
            {"metric_name": "Backend_Bound.Core_Bound", "value": 10.0, "unit": "% Slots"},
            {"metric_name": "Frontend_Bound", "value": 20.0, "unit": "% Clocks"},
        ]
        b = [
            {"metric_name": "Backend_Bound", "value": 40.0, "unit": "%"},
            {"metric_name": "Backend_Bound.Core_Bound", "value": 10.0, "unit": "%"},
            {"metric_name": "Backend_Bound.Memory_Bound", "value": 28.0, "unit": "%"},
            {"metric_name": "Frontend_Bound", "value": 25.0, "unit": "%"},
        ]
        lines = self._pair(a, b)
        # % Slots and % are both slot shares, so they are compared.
        assert next(ln for ln in lines if ln.startswith("Backend_Bound")).endswith("+0.0 pp")
        memory = lines.index(next(ln for ln in lines if "Memory_Bound" in ln))
        assert lines[memory - 1].startswith("Backend_Bound")  # B-only row sits under its parent
        assert lines[memory].split()[-3:] == ["—", "28.0%", "—"]
        assert next(ln for ln in lines if ln.startswith("Frontend_Bound")).endswith("— (units differ)")

    def test_useful_work_summary_marks_both_ways(self):
        a = [{"metric_name": "Retiring", "value": 40.0, "unit": "%"}]
        b = [{"metric_name": "Retiring", "value": 30.0, "unit": "%"}]
        summary = next(ln for ln in self._pair(a, b) if ln.startswith("Useful work"))
        assert summary.endswith("-10.0 pp ▼")

    def test_arrows_mean_better_or_worse(self):
        a = [
            {"metric_name": "Frontend_Bound", "value": 30.0, "unit": "%"},
            {"metric_name": "Retiring", "value": 40.0, "unit": "%"},
        ]
        b = [
            {"metric_name": "Frontend_Bound", "value": 20.0, "unit": "%"},
            {"metric_name": "Retiring", "value": 50.0, "unit": "%"},
        ]
        lines = self._pair(a, b)
        assert next(ln for ln in lines if ln.startswith("Frontend_Bound")).endswith("▲")
        assert next(ln for ln in lines if ln.startswith("Retiring")).endswith("▲")
        assert "▲ better / ▼ worse: slot-share change of at least 3 points" in lines
        worse = self._pair(b, a)
        assert next(ln for ln in worse if ln.startswith("Frontend_Bound")).endswith("▼")
        assert next(ln for ln in worse if ln.startswith("Retiring")).endswith("▼")

    def test_reports_mux_per_side(self, spr_metrics):
        lines = format_funnel_comparison(build_funnel(spr_metrics, 2), build_funnel(AMD_METRICS, 2)).splitlines()
        assert lines[-2].split() == ["Data", "quality:", "MUX", "3.0%", "—"]
        assert lines[-1].startswith("Some event groups were counted for only ~3%")
        assert not [ln for ln in lines[:-2] if "MUX" in ln]


class TestTreeText:
    def test_units_labelled_and_bars_only_for_slots(self):
        metrics = [
            {"metric_name": "Backend_Bound", "value": 40.0, "unit": "% Slots <"},
            {"metric_name": "Backend_Bound.Core_Bound", "value": 10.0, "unit": "% Slots"},
            {"metric_name": "Backend_Bound.Core_Bound.Divider", "value": 50.0, "unit": "% Clocks"},
            {"metric_name": "IPC", "value": 2.5, "unit": "Metric"},
        ]
        lines = format_tree_text(build_tree(metrics)).splitlines()
        divider = next(ln for ln in lines if "Divider" in ln)
        assert divider.split() == ["Divider", "50.0%", "of", "Clocks"]
        assert next(ln for ln in lines if "IPC" in ln).split() == ["IPC", "2.50", "Metric"]
        assert "█" in next(ln for ln in lines if "Backend_Bound" in ln)

    def test_tree_units_are_normalized(self):
        root = build_tree([{"metric_name": "Backend_Bound", "value": 40.0, "unit": "% Slots <"}])
        assert root.children["Backend_Bound"].unit == "% Slots"
        assert root.to_dict()["children"]["Backend_Bound"]["unit"] == "% Slots"

    def test_slot_only_output_is_unchanged(self):
        assert format_tree_text(build_tree(AMD_METRICS)) == (
            "  Frontend_Bound                             60.0%  ██████████████████\n"
            "    Fetch_Latency                              35.0%  ██████████\n"
            "  Retiring                                   40.0%  ████████████"
        )


class TestCompareRuns:
    def test_mux_delta_not_reported(self):
        a = [
            {"metric_name": "MUX", "value": 2.0, "unit": "%"},
            {"metric_name": "Backend_Bound", "value": 40.0, "unit": "% Slots <"},
        ]
        b = [
            {"metric_name": "MUX", "value": 90.0, "unit": "%"},
            {"metric_name": "Backend_Bound", "value": 30.0, "unit": "% Slots"},
        ]
        deltas = compare_runs(a, b)
        assert [d.metric_name for d in deltas] == ["Backend_Bound"]
        assert deltas[0].unit == "% Slots"

    def test_different_denominators_are_not_compared(self):
        a = [{"metric_name": "Backend_Bound.Memory_Bound.L1_Bound", "value": 10.0, "unit": "% Stalls"}]
        b = [{"metric_name": "Backend_Bound.Memory_Bound.L1_Bound", "value": 30.0, "unit": "% Slots"}]
        assert compare_runs(a, b) == []

    def test_slot_shares_compare_across_collectors(self):
        a = [{"metric_name": "Backend_Bound", "value": 55.4, "unit": "% Slots"}]
        b = [{"metric_name": "Backend_Bound", "value": 50.0, "unit": "%"}]
        (d,) = compare_runs(a, b)
        assert d.direction == "improved"

    def test_only_slot_shares_are_judged(self):
        a = [
            {"metric_name": "Backend_Bound", "value": 40.0, "unit": "% Slots"},
            {"metric_name": "Backend_Bound.Core_Bound.Divider", "value": 10.0, "unit": "% Clocks"},
            {"metric_name": "Retiring", "value": 17.0, "unit": "%"},
            {"metric_name": "Retiring.Heavy_Operations.Microcode_Sequencer", "value": 2.7, "unit": "%"},
        ]
        b = [
            {"metric_name": "Backend_Bound", "value": 42.0, "unit": "% Slots"},
            {"metric_name": "Backend_Bound.Core_Bound.Divider", "value": 30.0, "unit": "% Clocks"},
            {"metric_name": "Retiring", "value": 15.0, "unit": "%"},
            {"metric_name": "Retiring.Heavy_Operations.Microcode_Sequencer", "value": 6.7, "unit": "%"},
        ]
        # Slot shares first, even though the % Clocks delta is the largest.
        assert [d.metric_name for d in compare_runs(a, b)][-1] == "Backend_Bound.Core_Bound.Divider"
        by_name = {d.metric_name: d.direction for d in compare_runs(a, b)}
        assert by_name == {
            "Backend_Bound": "regressed",
            "Retiring": "regressed",
            "Retiring.Heavy_Operations.Microcode_Sequencer": "regressed",
            "Backend_Bound.Core_Bound.Divider": "changed",
        }
        assert summarize_comparison(compare_runs(a, b)).splitlines() == [
            "Regressions (3):",
            "  ↑ Retiring.Heavy_Operations.Microcode_Sequencer: 2.7% -> 6.7% (+4.0 pp)",
            "  ↑ Backend_Bound: 40.0% -> 42.0% (+2.0 pp)",
            "  ↓ Retiring: 17.0% -> 15.0% (-2.0 pp)",
            "Other denominators, not comparable with slot shares (not judged) (1):",
            "  ↑ Backend_Bound.Core_Bound.Divider: 10.0% of Clocks -> 30.0% of Clocks (+20.0 pp)",
            COMPOSITION_NOTE,
        ]

    def test_zero_delta_is_unchanged_and_not_listed(self):
        a = [
            {"metric_name": "Backend_Bound", "value": 40.0, "unit": "%"},
            {"metric_name": "Retiring.Light_Operations", "value": 20.0, "unit": "%"},
        ]
        b = [
            {"metric_name": "Backend_Bound", "value": 40.0, "unit": "%"},
            {"metric_name": "Retiring.Light_Operations", "value": 25.0, "unit": "%"},
        ]
        deltas = compare_runs(a, b, threshold=0.0)
        assert {d.metric_name: d.direction for d in deltas} == {
            "Backend_Bound": "unchanged",
            "Retiring.Light_Operations": "changed",
        }
        assert summarize_comparison(deltas).splitlines() == [
            "Useful-work mix (not judged) (1):",
            "  ↑ Retiring.Light_Operations: 20.0% -> 25.0% (+5.0 pp)",
        ]
        assert summarize_comparison([d for d in deltas if d.direction == "unchanged"]) == (
            "No significant differences found."
        )

    def test_delta_percent_is_none_from_zero(self):
        (d,) = compare_runs(
            [{"metric_name": "Backend_Bound", "value": 0.0, "unit": "%"}],
            [{"metric_name": "Backend_Bound", "value": 5.0, "unit": "%"}],
        )
        assert d.delta_percent is None

    def test_ties_are_ordered_by_name(self):
        metrics = [{"metric_name": n, "value": 10.0, "unit": "%"} for n in ("Frontend_Bound", "Backend_Bound")]
        assert [b.metric_name for b in find_bottlenecks(metrics)] == ["Backend_Bound", "Frontend_Bound"]
        branches = [
            {"metric_name": n, "value": 10.0, "unit": "%"}
            for n in ("Backend_Bound", "Backend_Bound.Z_Bound", "Backend_Bound.A_Bound")
        ]
        assert [b.metric_name for b in find_deepest_bottlenecks(branches)] == [
            "Backend_Bound.A_Bound",
            "Backend_Bound.Z_Bound",
        ]

    def test_long_groups_say_how_many_more(self):
        a = [{"metric_name": f"Backend_Bound.N{i}", "value": 10.0, "unit": "%"} for i in range(7)]
        b = [{"metric_name": f"Backend_Bound.N{i}", "value": 20.0, "unit": "%"} for i in range(7)]
        assert summarize_comparison(compare_runs(a, b)).splitlines()[-2] == "  ... +2 more"


class TestComparisonAndFunnelDetails:
    def test_sub_precision_delta_is_unchanged(self):
        (d,) = compare_runs(
            [{"metric_name": "Backend_Bound", "value": 40.00, "unit": "%"}],
            [{"metric_name": "Backend_Bound", "value": 40.04, "unit": "%"}],
            threshold=0.0,
        )
        assert d.direction == "unchanged"

    def test_estimate_note_in_funnel(self, spr_metrics):
        from topdown.units import ESTIMATE_NOTE

        assert ESTIMATE_NOTE in format_funnel_text(build_funnel(spr_metrics, max_level=3)).splitlines()
        assert ESTIMATE_NOTE not in format_funnel_text(build_funnel(AMD_METRICS))

    def test_zero_slot_row_has_no_bar(self):
        metrics = [*AMD_METRICS, {"metric_name": "Bad_Speculation", "value": 0.0, "unit": "%"}]
        row = next(ln for ln in format_funnel_text(build_funnel(metrics)).splitlines() if "Bad_Speculation" in ln)
        assert "█" not in row

    def test_units_differ_rows_show_each_denominator(self):
        a = [{"metric_name": "Frontend_Bound", "value": 20.0, "unit": "% Clocks"}]
        b = [{"metric_name": "Frontend_Bound", "value": 25.0, "unit": "%"}]
        row = next(
            ln
            for ln in format_funnel_comparison(build_funnel(a), build_funnel(b)).splitlines()
            if ln.startswith("Frontend_Bound")
        )
        assert "20.0% of Clocks" in row and row.endswith("— (units differ)")

    def test_comparison_rows_follow_the_baseline_order(self):
        a = [
            {"metric_name": "Backend_Bound", "value": 40.0, "unit": "%"},
            {"metric_name": "Frontend_Bound", "value": 30.0, "unit": "%"},
        ]
        b = [
            {"metric_name": "Backend_Bound", "value": 10.0, "unit": "%"},
            {"metric_name": "Frontend_Bound", "value": 50.0, "unit": "%"},
        ]
        rows = [ln.split()[0] for ln in format_funnel_comparison(build_funnel(a), build_funnel(b)).splitlines()[2:4]]
        assert rows == ["Backend_Bound", "Frontend_Bound"]

    def test_comparison_mux_caveat_uses_the_lower_run(self):
        a = [*AMD_METRICS, {"metric_name": "MUX", "value": 6.0, "unit": "%"}]
        b = [*AMD_METRICS, {"metric_name": "MUX", "value": 3.0, "unit": "%"}]
        text = format_funnel_comparison(build_funnel(a), build_funnel(b))
        assert "counted for only ~3% of an interval" in text

    def test_marked_comparison_carries_the_composition_note(self):
        a = [{"metric_name": "Backend_Bound", "value": 40.0, "unit": "%"}]
        b = [{"metric_name": "Backend_Bound", "value": 30.0, "unit": "%"}]
        assert COMPOSITION_NOTE in format_funnel_comparison(build_funnel(a), build_funnel(b)).splitlines()
