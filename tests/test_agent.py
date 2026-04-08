"""Tests for collection agent."""

import signal
from unittest.mock import patch, MagicMock

from topdown.service.agent import CollectionAgent
from topdown.config import TopdownConfig


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
