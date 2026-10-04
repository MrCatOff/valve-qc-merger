"""Studio entry point."""

from __future__ import annotations

import sys
from collections.abc import Sequence
from pathlib import Path


def selftest(mdl: Path | None) -> int:
    """Headless smoke test of a (frozen) build: the window comes up on Qt's
    offscreen platform, a project is created, ``mdl`` (if given) is imported
    with the in-process decompiler and turned into a viewport scene, and the
    bundled data the services need is present. Exit code 0 = healthy."""
    import os
    import tempfile

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    from valve_qc_merger.project import Project
    from valve_qc_merger.resources import data_root
    from valve_qc_merger.studio import theme
    from valve_qc_merger.studio.icons import ICON_DIR
    from valve_qc_merger.studio.main_window import MainWindow
    from valve_qc_merger.studio.scene import build_scene

    app = QApplication.instance() or QApplication(["valve-qc-studio"])
    theme.apply(app)  # QtSvg + the bundled icons must be in the build
    window = MainWindow()
    if not (ICON_DIR / "play.svg").exists():
        print(f"SELFTEST FAIL: icons missing in {ICON_DIR}", flush=True)
        return 1
    missing = [p for p in ("storage/hands/reference_hands.smd",
                           "storage/handswap/cso_hands.json.gz",
                           "storage/handswap/cso_reference_hands.smd",
                           "storage/players_donor") if not (data_root() / p).exists()]
    if missing:
        print(f"SELFTEST FAIL: bundled data missing: {missing}", flush=True)
        return 1
    with tempfile.TemporaryDirectory() as tmp:
        project = Project.create(Path(tmp) / "selftest")
        window.set_project(project)
        if mdl is not None:
            (asset,) = project.import_mdl(mdl)
            scene = build_scene(project.asset_dir(asset.name))
            print(f"imported {asset.name}: {len(scene.batches)} batches, "
                  f"{len(scene.sequences)} sequences", flush=True)
        window.close()
    app.processEvents()
    print("SELFTEST OK", flush=True)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Start the studio; an optional argument opens that project folder.
    ``--selftest [model.mdl]`` runs the headless smoke test instead."""
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
    if args and args[0] == "--selftest":
        return selftest(Path(args[1]) if len(args) > 1 else None)
    app = QApplication.instance() or QApplication([sys.argv[0], *args])
    app.setApplicationName("valve-qc-merger Studio")
    app.setApplicationVersion(__version__)
    app.setOrganizationName("valve-qc-merger")
    from valve_qc_merger.studio import theme
    theme.apply(app)
    window = MainWindow()
    window.show()
    if args and (Path(args[0]) / "project.toml").exists():
        window.open_project(Path(args[0]))
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
