"""Collector layer — auto-selects toplev (Intel), perf stat (ARM), or uprof_pcm (AMD)."""

import logging
import platform as _platform

logger = logging.getLogger(__name__)


def resolve_collector(config) -> str:
    """Determine which collector to use based on config and platform.

    Returns ``"toplev"``, ``"perf_stat"``, ``"uprof_pcm"``, or
    ``"perf_stat_amd"``.

    Dispatch order:
      * explicit ``config.collector`` wins
      * aarch64 -> ``perf_stat``
      * x86_64 + AuthenticAMD + AMDuProfPcm installed -> ``uprof_pcm``
      * x86_64 + AuthenticAMD + no AMDuProfPcm -> ``perf_stat_amd``
        (TMA-analog via stock perf Zen events; less accurate than uProf)
      * x86_64 default -> ``toplev``
    """
    if config.collector:
        return config.collector

    arch = _platform.machine()
    if arch == "aarch64":
        return "perf_stat"

    # x86_64: split Intel vs AMD via /proc/cpuinfo vendor_id.
    from topdown.collector.uprof_pcm import (
        detect_amd_vendor,
        check_uprof_pcm_available,
    )

    if detect_amd_vendor():
        uprof_path = getattr(config, "uprof_pcm_path", None)
        ok, _msg = check_uprof_pcm_available(uprof_path)
        if ok:
            return "uprof_pcm"
        # Fallback: AMD perf-stat events — less accurate but needs no extra install
        logger.info(
            "AuthenticAMD detected but AMDuProfPcm not installed; "
            "falling back to perf-stat AMD Zen events collector."
        )
        return "perf_stat_amd"

    return "toplev"


def make_runner(
    config,
    pids: list[int] | None,
    system_wide: bool,
    level: int,
):
    """Factory: return the right runner for the current platform/config.

    On Intel x86_64 (default): ``ToplevRunner`` wrapping pmu-tools/toplev.
    On aarch64 (default): ``PerfStatRunner`` wrapping ``perf stat --topdown``.
    On AMD x86_64 (default): ``UprofPcmRunner`` wrapping AMDuProfPcm.
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

    if collector == "uprof_pcm":
        from topdown.collector.uprof_pcm import UprofPcmRunner, UprofPcmOptions

        if level > 2:
            logger.warning(
                "AMDuProfPcm pipeline_util exposes L1+L2 metrics only; "
                "ignoring level=%d",
                level,
            )
        options = UprofPcmOptions(
            pids=pids,
            system_wide=system_wide,
            uprof_pcm_path=getattr(config, "uprof_pcm_path", None),
        )
        return UprofPcmRunner(options)

    if collector == "perf_stat_amd":
        from topdown.collector.perf_stat_amd import (
            PerfStatAmdRunner, PerfStatAmdOptions,
        )

        if level > 1:
            logger.warning(
                "perf-stat AMD events produce L1 metrics only; ignoring level=%d",
                level,
            )
        options = PerfStatAmdOptions(pids=pids, system_wide=system_wide)
        return PerfStatAmdRunner(options)

    # default: toplev (Intel)
    from topdown.collector.toplev import ToplevRunner, ToplevOptions

    options = ToplevOptions(level=level, pids=pids, system_wide=system_wide)
    return ToplevRunner(config.toplev_path, options)
