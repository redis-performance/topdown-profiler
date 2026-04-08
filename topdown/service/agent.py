"""Continuous collection agent."""

import logging
import signal
import time
from datetime import datetime, timezone

from topdown.collector.labels import collect_auto_labels, merge_labels
from topdown.collector.process_resolver import resolve_pids
from topdown.collector.toplev import ToplevRunner, ToplevOptions
from topdown.config import TopdownConfig
from topdown.storage import get_backend
from topdown.storage.models import Run, Sample

logger = logging.getLogger(__name__)


class CollectionAgent:
    """Runs periodic TMA collections as a daemon."""

    def __init__(
        self,
        process_name: str,
        level: int,
        interval_seconds: int,
        duration_seconds: int,
        config: TopdownConfig,
        custom_labels: dict[str, str] | None = None,
    ):
        self.process_name = process_name
        self.level = level
        self.interval_seconds = interval_seconds
        self.duration_seconds = duration_seconds
        self.config = config
        self.custom_labels = custom_labels or {}
        self._running = True
        self._collections = 0

    def run(self):
        """Main loop: collect, sleep, repeat. Handles SIGTERM/SIGINT."""
        signal.signal(signal.SIGTERM, self._handle_signal)
        signal.signal(signal.SIGINT, self._handle_signal)

        logger.info(
            "Agent starting: process=%s level=%d every=%ds duration=%ds",
            self.process_name, self.level, self.interval_seconds, self.duration_seconds,
        )

        while self._running:
            try:
                self._collect_once()
                self._collections += 1
                logger.info("Collection %d complete", self._collections)
            except Exception:
                logger.exception("Collection failed, will retry next interval")

            if self._running:
                self._sleep_interruptible(self.interval_seconds)

        logger.info("Agent stopped after %d collections", self._collections)

    def _collect_once(self):
        """Single collection cycle."""
        # Resolve PIDs
        pids = resolve_pids(self.process_name)
        if not pids:
            logger.warning("No process found matching '%s', skipping", self.process_name)
            return

        logger.info("Resolved %d PID(s) for '%s': %s", len(pids), self.process_name, pids)

        # Collect labels
        auto_labels = collect_auto_labels(
            self.process_name, pids, self.level, self.config.toplev_path,
        )
        all_labels = merge_labels(auto_labels, self.custom_labels)

        # Create run
        run = Run(
            process_name=self.process_name,
            level=self.level,
            labels=all_labels,
        )

        # Run toplev
        options = ToplevOptions(level=self.level, pids=pids)
        runner = ToplevRunner(self.config.toplev_path, options)

        start = time.time()
        toplev_samples = runner.run_and_parse(self.duration_seconds)
        elapsed = time.time() - start

        run.ended_at = datetime.now(timezone.utc)
        run.duration_seconds = elapsed

        if not toplev_samples:
            logger.warning("No samples collected this cycle")
            return

        # Store
        backend = get_backend(self.config)
        try:
            backend.insert_run(run)
            samples = [
                Sample(
                    run_id=run.run_id,
                    timestamp=s.timestamp or 0.0,
                    cpu=s.cpu,
                    metric_name=s.metric_name,
                    value=s.value,
                    unit=s.unit,
                    status=s.status,
                )
                for s in toplev_samples
            ]
            count = backend.insert_samples(samples)
            backend.update_run(run.run_id, run.ended_at, run.duration_seconds)
            logger.info("Stored run %s with %d samples", run.run_id[:12], count)
        finally:
            backend.close()

    def _sleep_interruptible(self, seconds: int):
        """Sleep in small increments so SIGTERM is handled promptly."""
        end = time.time() + seconds
        while self._running and time.time() < end:
            time.sleep(min(1.0, end - time.time()))

    def _handle_signal(self, signum, frame):
        logger.info("Received signal %d, shutting down gracefully...", signum)
        self._running = False

    @property
    def collections(self) -> int:
        return self._collections
