"""The Stax pilot owns its server and preserves reproducible run artifacts."""

import argparse
import json
from pathlib import Path
import platform
import subprocess
import sys
import textwrap

import pytest

from examples import stax_redis_pilot as PILOT


def fake_executable(path: Path, body: str) -> str:
    path.write_text(f"#!{sys.executable}\n" + textwrap.dedent(body))
    path.chmod(0o755)
    return str(path)


def test_benchmark_rates_and_balanced_order():
    assert PILOT.benchmark_rates('"SET","1200.5"\n"GET","1000"\n') == {"SET": 1200.5, "GET": 1000}
    csv_output = ('"test","rps","p50_latency_ms","p95_latency_ms","p99_latency_ms"\n'
                  '"SET","1200.5","0.2","0.5","0.8"\n')
    assert PILOT.benchmark_latency_ms(csv_output) == {"SET": {"p50": 0.2, "p95": 0.5, "p99": 0.8}}
    assert PILOT.schedule(2) == [False, True, True, False]
    assert PILOT.schedule(3).count(True) == PILOT.schedule(3).count(False) == 3
    with pytest.raises(ValueError, match="SET and GET"):
        PILOT.benchmark_rates('"SET","1200"\n')
    with pytest.raises(ValueError, match="positive"):
        PILOT.schedule(0)


@pytest.mark.parametrize("fail_second", [False, True])
def test_private_server_pilot_saves_and_reopens_each_capture(tmp_path, monkeypatch, fail_second):
    monkeypatch.setattr(PILOT.platform, "system", lambda: "Linux")
    monkeypatch.setenv("FAKE_BENCH_MARKER", str(tmp_path / "bench-count"))
    if fail_second:
        monkeypatch.setenv("FAKE_BENCH_FAIL_SECOND", "1")
    server = fake_executable(tmp_path / "fake-stax-server", """
        import os, signal, socket, sys, time
        if '--version' in sys.argv: print('fake stax server'); exit(0)
        sock = socket.socket(socket.AF_UNIX)
        sock.bind(os.environ['STAX_SERVER_SOCKET'])
        sock.listen()
        signal.signal(signal.SIGTERM, lambda *_: exit(0))
        while True: time.sleep(.05)
    """)
    stax = fake_executable(tmp_path / "fake-stax", """
        import pathlib, signal, sys, time
        args = sys.argv[1:]
        if args[0] == 'record':
            print('stax: started run 1', file=sys.stderr, flush=True)
            signal.signal(signal.SIGINT, lambda *_: exit(0))
            while True: time.sleep(.05)
        elif args[0] == 'save': pathlib.Path(args[1]).write_text('archive')
        elif args[0] == 'compare': print('{"ok":true}')
        elif args[0] == 'top': print('redis hot function')
        elif args[0] == 'stop': raise AssertionError('must not stop a shared run')
        else: print('no active run' if args[0] == 'status' else args[0])
    """)
    redis_server = fake_executable(tmp_path / "fake-redis-server", """
        import pathlib, signal, sys, time
        if '--version' in sys.argv: print('fake redis server'); exit(0)
        pathlib.Path(sys.argv[sys.argv.index('--unixsocket') + 1]).touch()
        signal.signal(signal.SIGTERM, lambda *_: exit(0))
        while True: time.sleep(.05)
    """)
    redis_cli = fake_executable(tmp_path / "fake-redis-cli", """
        import sys
        print('PONG' if 'ping' in sys.argv else 'OK')
    """)
    benchmark = fake_executable(tmp_path / "fake-redis-benchmark", """
        import os, pathlib, sys, time
        requests = sys.argv[sys.argv.index('-n') + 1]
        if requests == '1000':
            marker = pathlib.Path(os.environ['FAKE_BENCH_MARKER'])
            count = int(marker.read_text()) + 1 if marker.exists() else 1
            marker.write_text(str(count))
            if count == 2 and os.environ.get('FAKE_BENCH_FAIL_SECOND'): exit(7)
        time.sleep(.02)
        print('"SET","1000"')
        print('"GET","900"')
    """)
    output = tmp_path / "pilot"
    args = argparse.Namespace(output=output, stax_revision="a" * 40, redis_revision="b" * 40,
                              stax_build_flags="test", redis_build_flags="test", stax=stax, stax_server=server,
                              redis_server=redis_server, redis_cli=redis_cli, redis_benchmark=benchmark,
                              daemon_socket="/unused", pairs=2, requests=1000, warmup_requests=10,
                              clients=2, pipeline=1, frequency=100)
    if fail_second:
        with pytest.raises(RuntimeError, match="benchmark exited 7"):
            PILOT.pilot(args)
        assert "stax: started run " in (output / "round-02" / "record.stderr").read_text()
        assert len(json.loads((output / "metadata.json").read_text())["rounds"]) == 1
        return
    PILOT.pilot(args)

    result = json.loads((output / "metadata.json").read_text())
    assert [row["mode"] for row in result["rounds"]] == ["baseline", "stax", "stax", "baseline"]
    assert all(all(row["round_trip_equal"].values()) for row in result["rounds"] if row["mode"] == "stax")
    assert all(Path(row["archive"]).read_text() == "archive" for row in result["rounds"]
               if row["mode"] == "stax")
    assert result["summary"]["requests_per_command"] == 1000
    assert json.loads((output / "compare.stdout").read_text()) == {"ok": True}
    assert not list(output.rglob("stop.command.json"))


def test_absent_helper_fails_with_retained_preflight(tmp_path, monkeypatch):
    monkeypatch.setattr(PILOT.platform, "system", lambda: "Darwin")
    tool = fake_executable(tmp_path / "tool", "print('tool')\n")
    output = tmp_path / "pilot"
    args = argparse.Namespace(output=output, stax_revision="a" * 40, redis_revision="b" * 40,
                              stax_build_flags="test", redis_build_flags="test", stax=tool, stax_server=tool,
                              redis_server=tool, redis_cli=tool, redis_benchmark=tool,
                              daemon_socket=str(tmp_path / "missing.sock"), pairs=1, requests=1,
                              warmup_requests=1, clients=1, pipeline=1, frequency=100)
    with pytest.raises(RuntimeError, match="helper socket is absent"):
        PILOT.pilot(args)
    assert json.loads((output / "metadata.json").read_text())["host"]["machine"]


def test_m1_kpep_probe_detects_stax_order_and_nonsequential_mapping():
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        pytest.skip("Apple M1 KPEP probe requires Apple silicon")
    family = int(subprocess.check_output(["sysctl", "-n", "hw.cpufamily"], text=True).strip())
    if family != 0x1B588BB3:
        pytest.skip("This counter mapping is for Apple M1")
    script = Path(__file__).resolve().parents[1] / "examples" / "stax_m1_kpep_probe.py"
    result = subprocess.run([sys.executable, str(script)], capture_output=True, text=True, timeout=10, check=True)
    data = json.loads(result.stdout)
    assert data["stax_order"]["event_errors"]["BRANCH_MISPRED_NONSPEC"]["return_code"] == 13
    assert data["forced_first"]["event_to_sample_index"] == {
        "L1D_CACHE_MISS_LD": 2, "BRANCH_MISPRED_NONSPEC": 5,
    }
    assert data["hardware_access_verified"] is False
