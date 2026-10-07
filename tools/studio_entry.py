"""PyInstaller entry point for the valve-qc-studio GUI."""

import multiprocessing
import sys

from valve_qc_merger.studio.app import main

if __name__ == "__main__":
    # a frozen job process starts this executable again: let it run the job
    multiprocessing.freeze_support()
    sys.exit(main())
