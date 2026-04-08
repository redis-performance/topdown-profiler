"""Parse toplev CSV output with auto-format detection.

Toplev has 13+ CSV format variations depending on version/flags.
We auto-detect based on column count and content patterns.
"""

import re
import logging
from dataclasses import dataclass
from enum import Enum

logger = logging.getLogger(__name__)

# Regex for percentage values like "15.87%" or "15.87"
PERCENT_RE = re.compile(r"^-?[\d.]+%?$")
# Regex for timestamp (float seconds)
TIMESTAMP_RE = re.compile(r"^\d+\.\d+$")
# Regex for CPU identifiers: "0", "1", "CPU0", "S0-C0", "S0"
CPU_RE = re.compile(r"^(?:CPU)?(\d+)$|^S(\d+)(?:-C(\d+))?$")


class CsvFormat(Enum):
    """Known toplev CSV format variations."""

    # timestamp, metric_name, value, unit
    BASIC_4COL = "basic_4col"
    # timestamp, metric_name, value, unit, status
    BASIC_5COL = "basic_5col"
    # timestamp, cpu, metric_name, value, unit
    PERCPU_5COL = "percpu_5col"
    # timestamp, cpu, metric_name, value, unit, status
    PERCPU_6COL = "percpu_6col"
    # metric_name, value, unit (no timestamp, single snapshot)
    SNAPSHOT_3COL = "snapshot_3col"
    # Fallback
    UNKNOWN = "unknown"


@dataclass
class ToplevSample:
    """Single parsed metric sample."""

    timestamp: float | None
    cpu: int | None
    metric_name: str
    value: float
    unit: str
    status: str

    @property
    def level(self) -> int:
        """Infer TMA level from metric name depth."""
        return self.metric_name.count(".") + 1


def detect_format(lines: list[str], delimiter: str = ",") -> CsvFormat:
    """Detect CSV format from first few non-comment lines."""
    data_lines = [ln for ln in lines if ln.strip() and not ln.startswith("#")]
    if not data_lines:
        return CsvFormat.UNKNOWN

    sample = data_lines[0]
    cols = sample.split(delimiter)
    ncols = len(cols)

    # Check if first column is a timestamp
    has_timestamp = bool(TIMESTAMP_RE.match(cols[0].strip())) if ncols > 0 else False

    if ncols == 3 and not has_timestamp:
        return CsvFormat.SNAPSHOT_3COL

    if ncols == 4 and has_timestamp:
        return CsvFormat.BASIC_4COL

    if ncols == 5:
        if has_timestamp:
            # Is col[1] a CPU identifier?
            col1 = cols[1].strip()
            if CPU_RE.match(col1) or col1.isdigit():
                return CsvFormat.PERCPU_5COL
            return CsvFormat.BASIC_5COL

    if ncols == 6 and has_timestamp:
        return CsvFormat.PERCPU_6COL

    # Try harder: check multiple lines for consistency
    if ncols >= 4 and has_timestamp:
        if ncols == 5:
            return CsvFormat.BASIC_5COL
        if ncols >= 6:
            return CsvFormat.PERCPU_6COL

    return CsvFormat.UNKNOWN


def parse_cpu(raw: str) -> int | None:
    """Parse CPU identifier to integer."""
    raw = raw.strip()
    m = CPU_RE.match(raw)
    if m:
        if m.group(1) is not None:
            return int(m.group(1))
        if m.group(3) is not None:
            return int(m.group(3))
        if m.group(2) is not None:
            return int(m.group(2))
    try:
        return int(raw)
    except ValueError:
        return None


def parse_value(raw: str) -> float:
    """Parse a value field, stripping '%' suffix."""
    raw = raw.strip().rstrip("%")
    try:
        return float(raw)
    except ValueError:
        return 0.0


def parse_line(line: str, fmt: CsvFormat, delimiter: str = ",") -> ToplevSample | None:
    """Parse a single CSV line given the detected format."""
    line = line.strip()
    if not line or line.startswith("#"):
        return None

    cols = line.split(delimiter)
    cols = [c.strip() for c in cols]

    try:
        if fmt == CsvFormat.BASIC_4COL:
            # timestamp, metric, value, unit
            return ToplevSample(
                timestamp=float(cols[0]),
                cpu=None,
                metric_name=cols[1],
                value=parse_value(cols[2]),
                unit=cols[3],
                status="",
            )

        elif fmt == CsvFormat.BASIC_5COL:
            # timestamp, metric, value, unit, status
            return ToplevSample(
                timestamp=float(cols[0]),
                cpu=None,
                metric_name=cols[1],
                value=parse_value(cols[2]),
                unit=cols[3],
                status=cols[4] if len(cols) > 4 else "",
            )

        elif fmt == CsvFormat.PERCPU_5COL:
            # timestamp, cpu, metric, value, unit
            return ToplevSample(
                timestamp=float(cols[0]),
                cpu=parse_cpu(cols[1]),
                metric_name=cols[2],
                value=parse_value(cols[3]),
                unit=cols[4],
                status="",
            )

        elif fmt == CsvFormat.PERCPU_6COL:
            # timestamp, cpu, metric, value, unit, status
            return ToplevSample(
                timestamp=float(cols[0]),
                cpu=parse_cpu(cols[1]),
                metric_name=cols[2],
                value=parse_value(cols[3]),
                unit=cols[4],
                status=cols[5] if len(cols) > 5 else "",
            )

        elif fmt == CsvFormat.SNAPSHOT_3COL:
            # metric, value, unit
            return ToplevSample(
                timestamp=None,
                cpu=None,
                metric_name=cols[0],
                value=parse_value(cols[1]),
                unit=cols[2],
                status="",
            )

    except (IndexError, ValueError) as e:
        logger.warning("Failed to parse line: %s (%s)", line, e)
        return None

    return None


def parse_output(text: str, delimiter: str = ",") -> list[ToplevSample]:
    """Parse complete toplev CSV output text."""
    lines = text.strip().splitlines()
    data_lines = [ln for ln in lines if ln.strip() and not ln.startswith("#")]
    if not data_lines:
        return []

    fmt = detect_format(data_lines, delimiter)
    logger.debug("Detected CSV format: %s", fmt)

    samples = []
    for line in data_lines:
        sample = parse_line(line, fmt, delimiter)
        if sample and sample.metric_name:
            samples.append(sample)

    return samples


def parse_stream(lines: list[str], delimiter: str = ",") -> list[ToplevSample]:
    """Parse a list of CSV lines (streaming-friendly)."""
    data_lines = [ln for ln in lines if ln.strip() and not ln.startswith("#")]
    if not data_lines:
        return []

    fmt = detect_format(data_lines[:5], delimiter)

    samples = []
    for line in data_lines:
        sample = parse_line(line, fmt, delimiter)
        if sample and sample.metric_name:
            samples.append(sample)

    return samples
