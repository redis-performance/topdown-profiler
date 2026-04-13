"""Collector layer — auto-selects between toplev (Intel) and perf stat (ARM)."""

import logging
import platform as _platform

logger = logging.getLogger(__name__)


def resolve_collector(config) -> str:
    """Determine which collector to use based on config and platform.

    Returns ``"toplev"`` or ``"perf_stat"``.
    """
    if config.collector:
        return config.collector

    arch = _platform.machine()
    if arch == "aarch64":
        return "perf_stat"
    return "toplev"


def make_runner(
    config,
    pids: list[int] | None,
    system_wide: bool,
    level: int,
):
    """Factory: return the right runner for the current platform/config.

    On x86_64 (default): ``ToplevRunner`` wrapping pmu-tools/toplev.
    On aarch64 (default): ``PerfStatRunner`` wrapping ``perf stat --topdown``.
    Override with ``TOPDOWN_COLLECTOR`` env var or ``config.collector``.
    """
    collector = resolve_collector(config)

    if collector == "perf_stat":
        from topdown.collector.perf_stat import PerfStatRunner, PerfStatOptions

        if level > 1:
            logger.warning(
                "perf stat --topdown only supports L1; ignoring level=%d",
                level,
            )
        options = PerfStatOptions(pids=pids, system_wide=system_wide)
        return PerfStatRunner(options)
    else:
        from topdown.collector.toplev import ToplevRunner, ToplevOptions

        options = ToplevOptions(level=level, pids=pids, system_wide=system_wide)
        return ToplevRunner(config.toplev_path, options)
