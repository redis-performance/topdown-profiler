"""Source-checkout entry point for topdown.apple_gpu_report."""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from topdown.apple_gpu_report import main  # noqa: E402

if __name__ == "__main__":
    main()
