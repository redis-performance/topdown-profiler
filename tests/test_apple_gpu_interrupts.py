"""A terminal interrupt must not kill a GPU client while its native operation runs."""

import json
import os
import signal
import subprocess
import sys
import time

import pytest


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX process groups")
@pytest.mark.parametrize("phase", ["startup", "replay"])
@pytest.mark.parametrize("interrupts", [1, 2])
def test_cli_interrupt_finishes_native_command_then_cleans_only_its_session(tmp_path, phase, interrupts):
    harness = tmp_path / "gpu-cli.py"
    harness.write_text('''import json,signal,subprocess,sys
from pathlib import Path
from topdown.collector import apple_gpu,apple_runtime
root=Path(sys.argv[1]);phase=sys.argv[2]
trace=root/'test.gputrace';trace.mkdir()
original=apple_runtime.run
handler=signal.getsignal(signal.SIGINT)
program=''' + repr('''import os,json,sys,time
from pathlib import Path
root=Path(sys.argv[1]);phase=sys.argv[2]
print("Session 42 created." if phase=="startup" else "profiling",flush=True)
(root/'client.json').write_text(json.dumps({'pid':os.getpid(),'pgid':os.getpgrp(),'stdin_is_tty':os.isatty(0)}))
while not (root/'release').exists():time.sleep(.01)
(root/'client-finished').write_text('finished')
print('native-command-finished',flush=True)
''') + '''
def run(command,**kwargs):
    if '--terminate' in command:
        (root/'terminated.json').write_text(json.dumps(command))
        return subprocess.CompletedProcess(command,0,'terminated','')
    if '-t' in command and phase=='replay':
        return subprocess.CompletedProcess(command,0,'Session 42 created.\\n','')
    return original([sys.executable,'-u','-c',program,str(root),phase],**kwargs)
apple_runtime.run=run
apple_gpu.capture_environment=lambda:{'system':'Darwin','arch':'arm64'}
try:
    apple_gpu.collect(trace,root/'replay')
except KeyboardInterrupt:
    assert signal.getsignal(signal.SIGINT)==handler
    (root/'handler-restored').write_text('restored')
    raise SystemExit(130)
''')
    import pty

    terminal, input_fd = pty.openpty()
    try:
        cli = subprocess.Popen([sys.executable, str(harness), str(tmp_path), phase], stdin=input_fd,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
    finally:
        os.close(input_fd)
    try:
        deadline = time.monotonic() + 5
        while not (tmp_path / "client.json").exists() and time.monotonic() < deadline:
            if cli.poll() is not None:
                break
            time.sleep(.01)
        assert (tmp_path / "client.json").exists(), cli.communicate(timeout=2)
        identity = json.loads((tmp_path / "client.json").read_text())
        for _ in range(interrupts):
            os.killpg(cli.pid, signal.SIGINT)
            time.sleep(.05)
        assert cli.poll() is None, "CLI interrupted its active native command instead of waiting for safe cleanup"
        (tmp_path / "release").touch()
        stdout, stderr = cli.communicate(timeout=5)
        assert cli.returncode == 130, (stdout, stderr)
        assert identity["pgid"] == identity["pid"] and identity["pgid"] != cli.pid
        assert identity["stdin_is_tty"] is False
        assert (tmp_path / "client-finished").exists()
        assert (tmp_path / "handler-restored").exists()
        assert json.loads((tmp_path / "terminated.json").read_text()) == ["xcrun", "gpudebug", "--terminate", "42"]
        assert not (tmp_path / "replay/profile.json").exists()
        captured = "".join(path.read_text() for path in (tmp_path / "replay").glob("*.stdout"))
        assert "native-command-finished" in captured
    finally:
        os.close(terminal)
        (tmp_path / "release").touch()
        if cli.poll() is None:
            cli.terminate()
            try:
                cli.wait(timeout=3)
            except subprocess.TimeoutExpired:
                cli.kill()
                cli.wait(timeout=3)
        if cli.stdout:
            cli.stdout.close()
        if cli.stderr:
            cli.stderr.close()


@pytest.mark.parametrize("handler", [signal.SIG_IGN, signal.SIG_DFL])
def test_explicit_noncallable_interrupt_handlers_are_preserved(monkeypatch, handler):
    from unittest.mock import Mock
    from topdown.collector import apple_gpu

    change = Mock()
    monkeypatch.setattr(signal, "getsignal", lambda signum: handler)
    monkeypatch.setattr(signal, "signal", change)
    with apple_gpu._defer_keyboard_interrupt():
        pass
    change.assert_not_called()


def test_deferred_interrupt_restores_custom_handler_after_command_failure(monkeypatch):
    from unittest.mock import Mock
    from topdown.collector import apple_gpu

    previous = Mock()
    change = Mock()
    monkeypatch.setattr(signal, "getsignal", lambda signum: previous)
    monkeypatch.setattr(signal, "signal", change)
    with pytest.raises(ValueError, match="native failure"):
        with apple_gpu._defer_keyboard_interrupt():
            change.call_args.args[1](signal.SIGINT, None)
            raise ValueError("native failure")
    previous.assert_not_called()
    assert change.call_args.args == (signal.SIGINT, previous)


def test_deferred_interrupt_calls_existing_handler_after_restoring_it(monkeypatch):
    from unittest.mock import Mock
    from topdown.collector import apple_gpu

    change = Mock()
    def previous(signum, frame):
        assert change.call_args.args == (signal.SIGINT, handler)
        assert signum == signal.SIGINT and frame is None
    handler = Mock(side_effect=previous)
    monkeypatch.setattr(signal, "getsignal", lambda signum: handler)
    monkeypatch.setattr(signal, "signal", change)
    with apple_gpu._defer_keyboard_interrupt():
        change.call_args.args[1](signal.SIGINT, None)
    handler.assert_called_once_with(signal.SIGINT, None)
    assert change.call_args.args == (signal.SIGINT, handler)
