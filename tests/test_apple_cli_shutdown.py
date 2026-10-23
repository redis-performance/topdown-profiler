"""SIGTERM must release an Apple CLI capture without controlling borrowed PIDs."""

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time

import pytest
from typer.testing import CliRunner

from topdown.cli import app


@pytest.mark.parametrize("arguments", [["doctor"], ["soc", "--duration", "0", "--output", "unused"]])
def test_apple_cli_restores_custom_sigterm_handler(monkeypatch, arguments):
    monkeypatch.setattr("topdown.apple_cli.doctor", lambda: {"supported": False})
    def handler(signum, frame):
        pass
    previous = signal.signal(signal.SIGTERM, handler)
    try:
        CliRunner().invoke(app, ["apple", *arguments])
        assert signal.getsignal(signal.SIGTERM) is handler
    finally:
        signal.signal(signal.SIGTERM, previous)


def test_embedded_cli_in_worker_thread_keeps_main_thread_handlers(monkeypatch):
    monkeypatch.setattr("topdown.apple_cli.doctor", lambda: {"supported": False})
    previous = signal.getsignal(signal.SIGTERM)
    results = []
    thread = threading.Thread(target=lambda: results.append(CliRunner().invoke(app, ["apple", "doctor"])))
    thread.start()
    thread.join(3)
    assert not thread.is_alive()
    assert len(results) == 1 and results[0].exit_code == 1
    assert '"supported": false' in results[0].output
    assert signal.getsignal(signal.SIGTERM) == previous


def test_termination_outside_a_capture_exits_143_and_restores_handler(monkeypatch):
    previous = signal.getsignal(signal.SIGTERM)

    def doctor():
        signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)

    monkeypatch.setattr("topdown.apple_cli.doctor", doctor)
    result = CliRunner().invoke(app, ["apple", "doctor"])
    assert result.exit_code == 143
    assert signal.getsignal(signal.SIGTERM) == previous


@pytest.mark.skipif(os.name != "posix", reason="POSIX target ownership")
@pytest.mark.parametrize("borrowed", [False, True])
@pytest.mark.parametrize("ignore_term", [False, True])
def test_cli_sigterm_reaps_owned_target_and_retains_borrowed_target(tmp_path, borrowed, ignore_term):
    pidfile, observed = tmp_path / "target.pid", tmp_path / "observed"
    program = ("import os,signal,sys,time;from pathlib import Path;"
               + ("signal.signal(signal.SIGTERM,signal.SIG_IGN);" if ignore_term else "")
               + "Path(sys.argv[1]).write_text(str(os.getpid()));time.sleep(30)")
    borrowed_process = subprocess.Popen([sys.executable, "-c", program, str(pidfile)]) if borrowed else None
    arguments = (["--pid", str(borrowed_process.pid), "--duration", "30"] if borrowed else
                 ["--", sys.executable, "-c", program, str(pidfile)])
    harness = '''
from pathlib import Path
from types import SimpleNamespace
import sys,time
from topdown.cli import app_main
from topdown.collector import apple_stat
root=Path(sys.argv[1])
def read(pid):
    (root/'observed').touch()
    elapsed=time.monotonic_ns()
    return dict(instructions=elapsed,cycles=elapsed,user_time_ns=elapsed,system_time_ns=0,
                process_start_mach=10,process_exit_mach=0)
apple_stat._require_host=lambda:None
apple_stat._UsageReader=lambda:SimpleNamespace(flavor_id=4,timebase=SimpleNamespace(numer=1,denom=1),read=read)
sys.argv=['topdown','apple','stat','--output',str(root/'capture'),*sys.argv[2:]]
app_main()
'''
    target = None
    with (tmp_path / "stdout").open("w") as output, (tmp_path / "stderr").open("w") as errors:
        cli = subprocess.Popen([sys.executable, "-c", harness, str(tmp_path), *arguments], stdout=output,
                               stderr=errors, start_new_session=True, cwd=Path(__file__).resolve().parents[1])
        try:
            deadline = time.monotonic() + 6
            while time.monotonic() < deadline:
                if pidfile.exists() and (not borrowed or observed.exists()):
                    break
                time.sleep(.01)
            assert pidfile.exists()
            if borrowed:
                assert observed.exists()
            target = int(pidfile.read_text())
            cli.send_signal(signal.SIGTERM)
            if ignore_term and not borrowed:
                time.sleep(.1)
                cli.send_signal(signal.SIGTERM)
            assert cli.wait(timeout=8) == 143, (tmp_path / "stderr").read_text()
            report = json.loads((tmp_path / "capture/stat.json").read_text())
            assert report["status"] == "interrupted" and report["exit_code"] == 130
            assert "cleanup_error" not in report
            if borrowed:
                assert borrowed_process.poll() is None
            else:
                with pytest.raises(ProcessLookupError):
                    os.kill(target, 0)
        finally:
            if cli.poll() is None:
                cli.kill()
                cli.wait(timeout=3)
            if borrowed_process is not None:
                borrowed_process.kill()
                borrowed_process.wait(timeout=3)
            elif target is not None:
                try:
                    os.kill(target, signal.SIGKILL)
                except ProcessLookupError:
                    pass


def test_deferred_termination_takes_precedence_over_a_later_interrupt(monkeypatch):
    from topdown.collector import apple_gpu

    current = {}
    delivered = []
    handlers = {signum: lambda received, frame: delivered.append(received)
                for signum in (signal.SIGINT, signal.SIGTERM)}
    monkeypatch.setattr(signal, "getsignal", handlers.__getitem__)
    monkeypatch.setattr(signal, "signal", lambda signum, handler: current.__setitem__(signum, handler))
    with apple_gpu._defer_keyboard_interrupt():
        current[signal.SIGTERM](signal.SIGTERM, None)
        current[signal.SIGINT](signal.SIGINT, None)
        assert delivered == []
    assert delivered == [signal.SIGTERM]
    assert current == handlers
