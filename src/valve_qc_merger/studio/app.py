"""Studio entry point."""

from __future__ import annotations

import sys
from collections.abc import Sequence
from pathlib import Path


def main(argv: Sequence[str] | None = None) -> int:
    """Start the studio; an optional argument opens that project folder."""
    try:
        from PySide6.QtWidgets import QApplication
    except ImportError:
        print("The studio needs PySide6: pip install 'valve-qc-merger[studio]'",
              file=sys.stderr)
        return 2
    from PySide6.QtGui import QSurfaceFormat

    from valve_qc_merger import __version__
    from valve_qc_merger.studio.main_window import MainWindow
    from valve_qc_merger.studio.renderer import gl_format

    # GL 3.3 core for every context, before the application exists (macOS
    # otherwise hands out a legacy 2.1 context)
    QSurfaceFormat.setDefaultFormat(gl_format())
    args = list(sys.argv[1:] if argv is None else argv)
    app = QApplication.instance() or QApplication([sys.argv[0], *args])
    app.setApplicationName("valve-qc-merger Studio")
    app.setApplicationVersion(__version__)
    app.setOrganizationName("valve-qc-merger")
    app.setStyle("Fusion")
    window = MainWindow()
    window.show()
    if args and (Path(args[0]) / "project.toml").exists():
        window.open_project(Path(args[0]))
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
