"""PyInstaller entry point for the standalone valve-qc-merger executable."""

import sys

from valve_qc_merger.cli import main

if __name__ == "__main__":
    sys.exit(main())
