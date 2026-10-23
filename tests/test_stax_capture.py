"""Real subprocess ownership and cancellation using isolated fake Stax tools."""

import json
import os
from pathlib import Path
import subprocess
import sys
import stat
import threading
import time
from types import SimpleNamespace

import pytest

from tests.test_stax_archive import archive_document
from topdown.collector import stax
from topdown.collector.apple_runtime import _STOP
from topdown.stax_archive import StaxError

TOOL = r"""
import json,os,pathlib,signal,socket,sys,time
root=pathlib.Path(os.environ['STAX_TEST_ROOT'])
private=pathlib.Path(os.environ['STAX_SERVER_SOCKET']).parent
def log(kind):
    with (root/'events.jsonl').open('a') as f:
        f.write(json.dumps({'kind':kind,'pid':os.getpid(),'socket':os.environ['STAX_SERVER_SOCKET'],
                            'ws':os.environ['STAX_SERVER_WS_BIND'],'args':sys.argv[1:]})+'\n')
log(pathlib.Path(sys.argv[0]).name)
def terminate(sig,frame):
    log('signal-'+str(sig));raise SystemExit(0)
signal.signal(signal.SIGTERM,terminate)
mode=os.environ.get('STAX_TEST_MODE','normal')
if pathlib.Path(sys.argv[0]).name=='stax-server':
    if mode=='server-exit':raise SystemExit(7)
    s=socket.socket(socket.AF_UNIX);s.bind(os.environ['STAX_SERVER_SOCKET']);s.listen()
    while True:time.sleep(.02)
command=sys.argv[1]
if command=='record':
    (private/'target-pid').write_text(sys.argv[sys.argv.index('--pid')+1] if '--pid' in sys.argv else '42')
    if mode=='record-fail':print('permission denied',file=sys.stderr);raise SystemExit(7)
    if mode=='hang':
        while not (private/'stop').exists():time.sleep(.02)
    else:time.sleep(.05)
elif command=='stop':(private/'stop').touch()
elif command=='save':
    if mode=='save-fail':print('disk full',file=sys.stderr);raise SystemExit(8)
    dest=pathlib.Path(sys.argv[2]);dest.mkdir()
    document=json.loads((root/'source.json').read_text())
    document['runs'][0]['target_pid']=int((private/'target-pid').read_text())
    files={key:key+'.json' for key in ('aggregator','binaries','target_ingest','observability')}
    for key,name in files.items():(dest/name).write_text(json.dumps(document[key]))
    manifest={key:document[key] for key in ('format_version','saved_at_unix_ns','provenance','runs')}
    manifest['files']=files;(dest/'manifest.json').write_text(json.dumps(manifest))
"""


@pytest.fixture
def fake_stax(tmp_path, monkeypatch):
    root = Path("/tmp") / ("stax-test-" + tmp_path.name[-20:] + "-" + str(os.getpid()))
    root.mkdir(exist_ok=True)
    for name in ("stax", "stax-server"):
        path = root / name
        path.write_text(f"#!{sys.executable}\n" + TOOL)
        path.chmod(0o700)
    (root / "source.json").write_text(json.dumps(archive_document()))
    (root / "events.jsonl").write_text("")
    monkeypatch.setenv("TOPDOWN_STAX_PATH", str(root / "stax"))
    monkeypatch.setenv("TOPDOWN_STAX_SERVER_PATH", str(root / "stax-server"))
    monkeypatch.setenv("STAX_TEST_ROOT", str(root))
    monkeypatch.setenv("STAX_SERVER_SOCKET", str(root / "unrelated-server.sock"))
    monkeypatch.setenv("STAX_SERVER_WS_BIND", "127.0.0.1:8080")
    monkeypatch.setattr(stax.platform, "system", lambda: "Linux")
    yield root
    import shutil

    shutil.rmtree(root)


def events(root):
    return [json.loads(line) for line in (root / "events.jsonl").read_text().splitlines()]


def assert_reaped(root):
    for pid in {event["pid"] for event in events(root)}:
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)


def test_owned_server_and_attached_target_survive_capture(fake_stax, tmp_path):
    target = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(30)"])
    try:
        path, metadata = stax.record_profile(pid=target.pid, output=tmp_path / "capture", duration_seconds=1)
        assert (path / "manifest.json").is_file()
        assert metadata["status"] == "completed"
        assert target.poll() is None
        records = events(fake_stax)
        assert all(e["socket"] != str(fake_stax / "unrelated-server.sock") for e in records)
        assert all(e["ws"] == "127.0.0.1:0" for e in records)
        assert not Path(records[0]["socket"]).parent.exists()
        assert_reaped(fake_stax)
    finally:
        target.terminate()
        target.wait(timeout=3)


@pytest.mark.parametrize(
    "mode,message", [("server-exit", "startup"), ("record-fail", "recorder failed"), ("save-fail", "disk full")]
)
def test_failures_reap_server_and_preserve_logs(fake_stax, tmp_path, monkeypatch, mode, message):
    monkeypatch.setenv("STAX_TEST_MODE", mode)
    output = tmp_path / "capture"
    with pytest.raises(StaxError, match=message):
        stax.record_profile(pid=42, output=output, duration_seconds=1)
    assert json.loads((output / "capture.json").read_text())["status"] == "failed"
    assert (output / "server.log").exists()
    assert_reaped(fake_stax)


def test_cooperative_cancellation_stops_only_owned_recording(fake_stax, tmp_path, monkeypatch):
    monkeypatch.setenv("STAX_TEST_MODE", "hang")
    stop = threading.Event()
    token = _STOP.set(stop)
    timer = threading.Timer(0.3, stop.set)
    timer.start()
    try:
        with pytest.raises(StaxError, match="cancelled"):
            stax.record_profile(pid=42, output=tmp_path / "capture", duration_seconds=1)
    finally:
        timer.cancel()
        _STOP.reset(token)
    assert any(e["args"] == ["stop"] for e in events(fake_stax))
    assert (tmp_path / "capture" / "partial-archive" / "manifest.json").is_file()
    assert_reaped(fake_stax)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"pid": 0},
        {"pid": True},
        {"pid": 2**32},
        {"duration_seconds": 0},
        {"duration_seconds": 1.5},
        {"duration_seconds": 3601},
        {"frequency_hz": 0},
        {"frequency_hz": 10001},
        {"frequency_hz": True},
    ],
)
def test_invalid_options_do_not_start_tools(fake_stax, tmp_path, kwargs):
    options = {"pid": 42, "output": tmp_path / "capture", **kwargs}
    with pytest.raises(StaxError):
        stax.record_profile(**options)
    assert events(fake_stax) == []


def test_existing_output_never_overwritten(fake_stax, tmp_path):
    with pytest.raises(FileExistsError):
        stax.record_profile(pid=42, output=tmp_path)
    assert events(fake_stax) == []


def test_missing_tools_and_macos_helper_are_explicit(tmp_path, monkeypatch):
    monkeypatch.setenv("TOPDOWN_STAX_PATH", str(tmp_path / "absent"))
    monkeypatch.setenv("TOPDOWN_STAX_SERVER_PATH", str(tmp_path / "absent-server"))
    monkeypatch.setattr(stax.platform, "system", lambda: "Darwin")
    monkeypatch.setenv("TOPDOWN_STAX_DAEMON_SOCKET", str(tmp_path / "missing.sock"))
    result = stax.doctor()
    assert result["capture_prerequisites_present"] is False
    assert result["permissions_verified"] is False
    assert result["pmu_availability"] == "unverified"
    with pytest.raises(StaxError, match="privileged helper"):
        stax.record_profile(pid=42, output=tmp_path / "capture")
    assert not (tmp_path / "capture").exists()


def test_command_timeout_reaps_process(fake_stax, monkeypatch):
    env = {**os.environ, "STAX_SERVER_SOCKET": str(fake_stax / "owned.sock"), "STAX_SERVER_WS_BIND": "127.0.0.1:0"}
    monkeypatch.setenv("STAX_TEST_MODE", "hang")
    env["STAX_TEST_MODE"] = "hang"
    started = time.monotonic()
    with pytest.raises(StaxError, match="timed out"):
        stax._run([str(fake_stax / "stax"), "record"], env, 0.15)
    assert time.monotonic() - started < 3
    assert_reaped(fake_stax)


def test_owned_command_inherits_private_socket_and_is_reaped(fake_stax, tmp_path):
    result = tmp_path / "target.json"
    program = "import json,os,sys,time;from pathlib import Path;Path(sys.argv[1]).write_text(json.dumps({'pid':os.getpid(),'socket':os.environ['STAX_SERVER_SOCKET']}));time.sleep(30)"
    path, metadata = stax.record_profile(
        command=[sys.executable, "-c", program, str(result)], output=tmp_path / "capture", duration_seconds=1
    )
    target = json.loads(result.read_text())
    assert metadata["target_ownership"] == "launched"
    assert metadata["pid"] == target["pid"]
    assert metadata["target_exit_code"] != 0
    assert "before attachment" in metadata["startup_coverage"]
    assert target["socket"] == events(fake_stax)[0]["socket"]
    assert (path / "manifest.json").is_file()
    with pytest.raises(ProcessLookupError):
        os.kill(target["pid"], 0)
    assert_reaped(fake_stax)


def test_fast_command_is_retained_as_unreaped_cleanup_identity(fake_stax, tmp_path, monkeypatch):
    real_finish = stax._finish_target
    observed = []

    def finish(process):
        if process is not None and process.returncode is None:
            from topdown.collector.apple_stat import _peek_exit

            observed.append(_peek_exit(process.pid))
        return real_finish(process)

    monkeypatch.setattr(stax, "_finish_target", finish)
    _, metadata = stax.record_profile(
        command=[sys.executable, "-c", "raise SystemExit(7)"], output=tmp_path / "capture", duration_seconds=1
    )
    assert metadata["target_exit_code"] == 7
    assert observed[0] is not None
    assert_reaped(fake_stax)


@pytest.mark.parametrize("command", [[], "echo hi", [""], ["echo", "\x00"], [True], ["echo", 1], ["echo", "\ud800"]])
def test_invalid_command_never_starts_tools(fake_stax, tmp_path, command):
    with pytest.raises(StaxError, match="argv"):
        stax.record_profile(command=command, output=tmp_path / "capture")
    assert events(fake_stax) == []


def test_target_selection_is_exclusive(fake_stax, tmp_path):
    for options in ({}, {"pid": 42, "command": ["true"]}):
        with pytest.raises(StaxError, match="exactly one"):
            stax.record_profile(output=tmp_path / "capture", **options)
    assert events(fake_stax) == []


def test_recorder_failure_still_stops_owned_target(fake_stax, tmp_path, monkeypatch):
    monkeypatch.setenv("STAX_TEST_MODE", "record-fail")
    output = tmp_path / "capture"
    with pytest.raises(StaxError, match="permission denied"):
        stax.record_profile(command=[sys.executable, "-c", "import time;time.sleep(30)"], output=output)
    report = json.loads((output / "capture.json").read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(report["pid"], 0)
    assert_reaped(fake_stax)


@pytest.mark.parametrize("metadata_fails_on_success", [False, True])
def test_metadata_write_failure_does_not_mask_primary_error(
    fake_stax, tmp_path, monkeypatch, metadata_fails_on_success
):
    if not metadata_fails_on_success:
        monkeypatch.setenv("STAX_TEST_MODE", "record-fail")
    original = Path.write_text

    def write(path, *args, **kwargs):
        if path.name == "capture.json":
            raise OSError("metadata disk full")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", write)
    expected = "write capture metadata" if metadata_fails_on_success else "permission denied"
    with pytest.raises(StaxError, match=expected):
        stax.record_profile(pid=42, output=tmp_path / "capture", duration_seconds=1)
    assert_reaped(fake_stax)


@pytest.mark.parametrize("private,root_owned", [(True, True), (False, True), (True, False)])
def test_macos_helper_requires_private_directory_and_root_socket(fake_stax, monkeypatch, private, root_owned):
    endpoint = fake_stax / "helper" / "staxd.sock"
    monkeypatch.setenv("TOPDOWN_STAX_DAEMON_SOCKET", str(endpoint))
    monkeypatch.setattr(stax.platform, "system", lambda: "Darwin")
    original = Path.lstat

    def inspect(path, *args, **kwargs):
        if path == endpoint:
            return SimpleNamespace(st_mode=stat.S_IFSOCK | 0o666, st_uid=0 if root_owned else 123456)
        if path == endpoint.parent:
            return SimpleNamespace(st_mode=stat.S_IFDIR | (0o700 if private else 0o755), st_uid=os.getuid())
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "lstat", inspect)
    result = stax.doctor()
    assert result["helper_socket_present"] is True
    assert result["helper_socket_private"] is (private and root_owned)
    assert result["capture_prerequisites_present"] is (private and root_owned)


def test_capture_lock_excludes_another_process_and_releases_after_failure():
    program = "from topdown.collector.stax import _capture_lock\nwith _capture_lock():\n print('acquired')\n"
    with stax._capture_lock():
        result = subprocess.run(
            [sys.executable, "-c", program],
            capture_output=True,
            text=True,
            cwd=Path(__file__).resolve().parents[1],
            timeout=3,
        )
        assert result.returncode != 0 and "Another Stax capture" in result.stderr
    result = subprocess.run(
        [sys.executable, "-c", program],
        capture_output=True,
        text=True,
        cwd=Path(__file__).resolve().parents[1],
        timeout=3,
    )
    assert result.returncode == 0 and "acquired" in result.stdout
