import multiprocessing

from valve_qc_merger.studio.app import main

if __name__ == "__main__":  # job processes import this module too: no second window
    multiprocessing.freeze_support()
    raise SystemExit(main())
