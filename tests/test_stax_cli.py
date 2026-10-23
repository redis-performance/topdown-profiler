"""Capture/storage commands and process ownership through the public CLI."""

import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import time

import pytest
from typer.testing import CliRunner

from tests.test_stax_capture import fake_stax  # noqa: F401
from topdown.cli import app


@pytest.fixture
def tools(request):
    return request.getfixturevalue("fake_stax")


def test_record_store_and_query(tools, tmp_path):
    result = CliRunner().invoke(
        app,
        [
            "stax",
            "record",
            "--pid",
            "42",
            "--output",
            str(tmp_path / "capture"),
            "--duration",
            "1",
            "--store",
            "--db",
            str(tmp_path / "db"),
            "--label",
            "test_name=cli",
        ],
    )
    assert result.exit_code == 0, result.output
    result = json.loads(result.stdout)
    assert result["labels"]["sampling_frequency_hz"] == "99"
    assert result["metadata"]["capture"]["target_ownership"] == "attached"


def test_record_without_store_retains_archive(tools, tmp_path):
    result = CliRunner().invoke(
        app, ["stax", "record", "--pid", "42", "--output", str(tmp_path / "capture"), "--duration", "1"]
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["archive"].endswith("/archive")


def test_storage_failure_keeps_capture(tools, tmp_path, monkeypatch):
    def fail(*args, **kwargs):
        raise sqlite3.OperationalError("database unavailable")

    monkeypatch.setattr("topdown.stax_cli._query", fail)
    result = CliRunner().invoke(
        app, ["stax", "record", "--pid", "42", "--output", str(tmp_path / "capture"), "--duration", "1", "--store"]
    )
    assert result.exit_code == 1
    assert "artifacts retained" in result.stderr and "database unavailable" in result.stderr
    assert (tmp_path / "capture/archive/manifest.json").is_file()


def test_labels_without_store_do_not_start_capture(tools, tmp_path):
    result = CliRunner().invoke(
        app, ["stax", "record", "--pid", "42", "--output", str(tmp_path / "capture"), "-L", "test_name=x"]
    )
    assert result.exit_code == 1 and "--store" in result.stderr
    assert not (tmp_path / "capture").exists()


def test_invalid_labels_are_reported_before_capture(tools, tmp_path):
    result = CliRunner().invoke(
        app, ["stax", "record", "--pid", "42", "--output", str(tmp_path / "capture"), "--store", "-L", "broken"]
    )
    assert result.exit_code == 1 and "key=value" in result.stderr
    assert not (tmp_path / "capture").exists()


def test_doctor_and_tma_collection_guidance(tools, monkeypatch):
    result = CliRunner().invoke(app, ["stax", "doctor"])
    assert result.exit_code == 0
    assert json.loads(result.stdout)["collector"] == "stax"
    monkeypatch.setenv("TOPDOWN_COLLECTOR", "stax")
    result = CliRunner().invoke(app, ["collect", "--process", "redis-server"])
    assert result.exit_code == 1 and "topdown stax record" in result.stdout


@pytest.mark.parametrize("borrowed,ignore_term", [(True, False), (False, False), (False, True)])
def test_sigterm_waits_for_cleanup_and_preserves_borrowed_pid(tools, tmp_path, monkeypatch, borrowed, ignore_term):
    monkeypatch.setenv("STAX_TEST_MODE", "hang")
    pidfile = tmp_path / "target.pid"
    program = (
        "import os,signal,sys,time;from pathlib import Path;"
        + ("signal.signal(signal.SIGTERM,signal.SIG_IGN);" if ignore_term else "")
        + "Path(sys.argv[1]).write_text(str(os.getpid()));time.sleep(30)"
    )
    target = subprocess.Popen([sys.executable, "-c", program, str(pidfile)]) if borrowed else None
    arguments = ["--pid", str(target.pid)] if borrowed else ["--", sys.executable, "-c", program, str(pidfile)]
    harness = "from topdown.collector import stax;stax.platform.system=lambda:'Linux';from topdown.cli import app_main;app_main()"
    cli = subprocess.Popen(
        [sys.executable, "-c", harness, "stax", "record", "--output", str(tmp_path / "capture"), *arguments],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
        cwd=Path(__file__).resolve().parents[1],
    )
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            rows = [json.loads(line) for line in (tools / "events.jsonl").read_text().splitlines()]
            if pidfile.exists() and any(row["args"] and row["args"][0] == "record" for row in rows):
                break
            time.sleep(0.01)
        assert pidfile.exists()
        target_pid = int(pidfile.read_text())
        cli.send_signal(signal.SIGTERM)
        if ignore_term:
            time.sleep(0.1)
            cli.send_signal(signal.SIGTERM)
        out, err = cli.communicate(timeout=10)
        assert cli.returncode == 143, (out, err)
        report = json.loads((tmp_path / "capture/capture.json").read_text())
        assert report["status"] == "interrupted"
        assert (tmp_path / "capture/partial-archive/manifest.json").is_file()
        if borrowed:
            assert target.poll() is None
        else:
            with pytest.raises(ProcessLookupError):
                os.kill(target_pid, 0)
        from tests.test_stax_capture import assert_reaped

        assert_reaped(tools)
    finally:
        if cli.poll() is None:
            cli.kill()
            cli.wait(timeout=3)
        if target is not None:
            target.terminate()
            target.wait(timeout=3)
