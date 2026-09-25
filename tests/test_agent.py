"""Tests for collection agent."""

import logging
import signal

from tests.conftest import TEST_DATA_DIR
from topdown.collector.csv_parser import parse_output
from topdown.service import agent as agent_module
from topdown.service.agent import CollectionAgent
from topdown.config import TopdownConfig
from topdown.storage.sqlite_backend import SQLiteBackend


class TestCollectionAgent:
    def test_signal_stops_agent(self, tmp_path):
        config = TopdownConfig(db_path=tmp_path / "test.db")
        agent = CollectionAgent(
            process_name="redis-server",
            level=2,
            interval_seconds=60,
            duration_seconds=10,
            config=config,
        )
        assert agent._running is True
        agent._handle_signal(signal.SIGTERM, None)
        assert agent._running is False

    def test_collections_counter(self, tmp_path):
        config = TopdownConfig(db_path=tmp_path / "test.db")
        agent = CollectionAgent(
            process_name="redis-server",
            level=2,
            interval_seconds=60,
            duration_seconds=10,
            config=config,
        )
        assert agent.collections == 0

    def test_custom_labels_stored(self, tmp_path):
        config = TopdownConfig(db_path=tmp_path / "test.db")
        agent = CollectionAgent(
            process_name="redis-server",
            level=2,
            interval_seconds=60,
            duration_seconds=10,
            config=config,
            custom_labels={"git_branch": "unstable"},
        )
        assert agent.custom_labels == {"git_branch": "unstable"}

    def test_collect_once_stores_clean_units_and_logs_full_run_id(self, tmp_path, monkeypatch, caplog):
        samples = parse_output((TEST_DATA_DIR / "toplev_csv_extended_spr.csv").read_text())

        class FakeRunner:
            def run_and_parse(self, duration_seconds):
                return samples

        monkeypatch.setattr(agent_module, "resolve_pids", lambda name: [4242])
        monkeypatch.setattr(agent_module, "resolve_collector", lambda config: "toplev")
        monkeypatch.setattr(agent_module, "collect_auto_labels", lambda *a, **k: {})
        monkeypatch.setattr(agent_module, "make_runner", lambda *a, **k: FakeRunner())

        config = TopdownConfig(db_path=tmp_path / "test.db")
        agent = CollectionAgent(
            process_name="redis-server",
            level=4,
            interval_seconds=60,
            duration_seconds=1,
            config=config,
        )
        with caplog.at_level(logging.INFO, logger="topdown.service.agent"):
            agent._collect_once()

        backend = SQLiteBackend(db_path=tmp_path / "test.db")
        (run,) = backend.list_runs()
        units = {s.unit for s in backend.get_samples(run.run_id)}
        backend.close()
        assert f"Stored run {run.run_id} with {len(samples)} samples" in caplog.text
        assert "% Slots" in units
        assert not [u for u in units if u.endswith("<")]


class TestSystemdService:
    def test_generate_unit_file(self):
        from topdown.service.systemd import generate_unit_file

        content = generate_unit_file(
            process_name="redis-server",
            level=3,
            every="5m",
            duration="30s",
        )
        assert "[Unit]" in content
        assert "redis-server" in content
        assert "level 3" in content or "--level 3" in content
        assert "Restart=on-failure" in content
        assert "[Install]" in content

    def test_preview(self):
        from topdown.service.systemd import get_unit_file_preview

        content = get_unit_file_preview(
            process_name="valkey-server",
            level=2,
            every="10m",
            duration="60s",
        )
        assert "valkey-server" in content
        assert "10m" in content
