"""Source-checkout entry point for topdown.collector.apple_gpu."""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from topdown.collector.apple_gpu import main  # noqa: E402

if __name__ == "__main__":
    main()
