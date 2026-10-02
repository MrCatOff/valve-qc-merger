"""PyInstaller entry point for the valve-qc-studio GUI."""

import sys

from valve_qc_merger.studio.app import main

if __name__ == "__main__":
    sys.exit(main())
