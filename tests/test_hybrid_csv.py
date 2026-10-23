"""Regression for the Intel hybrid capture recorded in the Linux Stax pilot."""

from pathlib import Path

import pytest

from topdown.collector.csv_parser import CsvFormat, detect_format, parse_output, parse_stream, sample_scope_labels
from topdown.collector.toplev import ToplevOptions, ToplevRunner
from topdown.config import TopdownConfig

CAPTURE = (Path(__file__).parent / "test_data/toplev_csv_hybrid_core.csv").read_text()


@pytest.mark.parametrize("header", [False, True])
@pytest.mark.parametrize("delimiter", [",", ";"])
def test_real_hybrid_group_does_not_shift_metric_value_or_unit(header, delimiter):
    text = CAPTURE if header else CAPTURE.splitlines()[1]
    text = text.replace(",", delimiter)
    assert detect_format(text.splitlines(), delimiter) == CsvFormat.EXTENDED_CPU_11COL
    for rows in (parse_output(text, delimiter), parse_stream(text.splitlines(), delimiter)):
        assert len(rows) == 1
        row = rows[0]
        assert row.metric_name == "Frontend_Bound" and row.value == 37.9 and row.unit == "% Slots"
        assert row.cpu is None and row.cpu_group == "core" and row.status == ""
        assert sample_scope_labels(rows) == {"pmu_group": "core"}


def test_numeric_extended_cpu_retains_cpu_id():
    row = parse_output(CAPTURE.replace(",core,", ",7,"))[0]
    assert row.cpu == 7 and row.cpu_group is None
    assert row.metric_name == "Frontend_Bound" and row.value == 37.9


def test_quoted_description_and_bottleneck_column():
    text = CAPTURE.replace(",% Slots,,frontend", ',% Slots,"description, with comma",frontend')
    text = text.replace(",100.0,,", ",100.0,*,")
    row = parse_output(text)[0]
    assert row.metric_name == "Frontend_Bound" and row.value == 37.9 and row.status == "*"


def test_mixed_groups_are_not_silently_averaged():
    text = CAPTURE + CAPTURE.splitlines()[1].replace(",core,", ",atom,")
    rows = parse_output(text)
    assert {row.cpu_group for row in rows} == {"core", "atom"}
    with pytest.raises(RuntimeError, match="mixed CPU/PMU groups"):
        sample_scope_labels(rows)


def test_group_scope_must_match_requested_cpu_type():
    with pytest.raises(RuntimeError, match="instead of requested atom"):
        sample_scope_labels(parse_output(CAPTURE), "atom")
    assert sample_scope_labels([], "core") == {"pmu_group": "core"}


def test_runner_selects_and_checks_one_hybrid_group(monkeypatch):
    runner = ToplevRunner("toplev.py", ToplevOptions(cpu_type="core"))
    assert runner.build_command()[-2:] == ["--cputype", "core"]
    monkeypatch.setattr(runner, "run", lambda duration: ("", CAPTURE))
    assert runner.run_and_parse(1)[0].cpu_group == "core"
    runner.options.cpu_type = "atom"
    with pytest.raises(RuntimeError, match="instead of requested atom"):
        runner.run_and_parse(1)
    runner.options.cpu_type = "invalid"
    with pytest.raises(RuntimeError, match="must be core or atom"):
        runner.build_command()


def test_configured_group_reaches_runner(monkeypatch):
    from topdown.collector import make_runner

    monkeypatch.setenv("TOPDOWN_TOPLEV_CPU_TYPE", "core")
    config = TopdownConfig.from_env()
    config.collector = "toplev"
    runner = make_runner(config, [42], False, 2)
    assert runner.options.cpu_type == "core"
    assert "--cputype" in runner.build_command()


@pytest.mark.parametrize("interface", ["cli", "mcp", "agent"])
@pytest.mark.parametrize("mixed", [False, True])
def test_group_scope_is_preserved_or_rejected_before_sql(interface, mixed, monkeypatch, tmp_path):
    from types import SimpleNamespace
    from typer.testing import CliRunner
    from topdown.cli import app
    from topdown import mcp_server
    from topdown.service import agent
    from topdown.storage.sqlite_backend import SQLiteBackend

    data = CAPTURE + (CAPTURE.splitlines()[1].replace(",core,", ",atom,") if mixed else "")
    runner = SimpleNamespace(run_and_parse=lambda seconds: parse_output(data))
    path = tmp_path / "capture.db"
    monkeypatch.setenv("TOPDOWN_DB_PATH", str(path))
    monkeypatch.setenv("TOPDOWN_COLLECTOR", "toplev")
    monkeypatch.setattr("topdown.collector.toplev.check_perf_permissions", lambda: (True, "ok"))
    monkeypatch.setattr("topdown.collector.toplev.check_toplev_available", lambda *a: True)
    monkeypatch.setattr("topdown.collector.process_resolver.resolve_pids", lambda *a: [42])
    monkeypatch.setattr("topdown.collector.labels.collect_auto_labels", lambda *a, **k: {})
    monkeypatch.setattr("topdown.collector.make_runner", lambda *a, **k: runner)
    if interface == "cli":
        result = CliRunner().invoke(app, ["collect", "--process", "redis-server", "--duration", "1s"])
        assert result.exit_code == (1 if mixed else 0), result.output
    elif interface == "mcp":
        result = mcp_server.collect_topdown("redis-server", duration_seconds=1)
        assert ("Error running collection" in result) is mixed
    else:
        monkeypatch.setattr(agent, "resolve_pids", lambda *a: [42])
        monkeypatch.setattr(agent, "collect_auto_labels", lambda *a, **k: {})
        monkeypatch.setattr(agent, "make_runner", lambda *a, **k: runner)
        worker = agent.CollectionAgent("redis-server", 2, 60, 1, TopdownConfig(db_path=path, collector="toplev"))
        if mixed:
            with pytest.raises(RuntimeError, match="mixed CPU/PMU"):
                worker._collect_once()
        else:
            worker._collect_once()
    backend = SQLiteBackend(path)
    backend.initialize()
    try:
        runs = backend.list_runs()
        if mixed:
            assert runs == []
        else:
            assert len(runs) == 1 and runs[0].labels["pmu_group"] == "core"
            (sample,) = backend.get_samples(runs[0].run_id)
            assert sample.metric_name == "Frontend_Bound" and sample.value == 37.9 and sample.unit == "% Slots"
    finally:
        backend.close()
