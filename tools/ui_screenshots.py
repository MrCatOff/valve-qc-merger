"""Screenshots of the studio's main screens, for before/after UI reviews.

    python tools/ui_screenshots.py OUT_DIR

Builds a throw-away project from tests/examples (a view model with its
swap-hands result, a category, player/world models and a merge-v build),
then grabs: the welcome screen, an asset, a swap-hands asset, the Bones
tab, a build, the Retarget dialog and the New build dialog. Runs on the
native platform (the viewport needs OpenGL); takes ~15 s.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def main(out: Path) -> int:
    from PySide6.QtCore import QSettings, QTimer
    from PySide6.QtGui import QSurfaceFormat
    from PySide6.QtWidgets import QApplication

    from valve_qc_merger.studio.renderer import gl_format
    QSurfaceFormat.setDefaultFormat(gl_format())
    app = QApplication([])
    from valve_qc_merger.project import Build, Project
    from valve_qc_merger.services.base import CollectingReporter
    from valve_qc_merger.studio import theme
    from valve_qc_merger.studio.build_panel import NewBuildDialog
    from valve_qc_merger.studio.derive_dialog import DeriveDialog
    from valve_qc_merger.studio.main_window import MainWindow
    theme.apply(app)

    out.mkdir(parents=True, exist_ok=True)
    work = out / "_project"
    shutil.rmtree(work, ignore_errors=True)
    examples = ROOT / "tests" / "examples"
    project = Project.create(work / "pack")
    for model in ("v_anaconda", "pair_deagle/v_deagle", "player/p_anaconda",
                  "player/p_elite", "world/w_glockred"):
        project.import_decompiled(examples / model)
    project.derive_asset("v_anaconda", "hands", reporter=CollectingReporter())
    project.add_category("pistols")
    project.set_category(["v_anaconda"], "pistols")
    project.add_build(Build("view", "merge-v", retarget=True))

    window = MainWindow(QSettings(str(out / "_settings.ini"), QSettings.Format.IniFormat))
    window.resize(1500, 920)

    def snap(name: str) -> None:
        app.processEvents()
        window.grab().save(str(out / f"{name}.png"))

    def grab_dialog(dialog, name: str) -> None:  # noqa: ANN001
        dialog.show()
        app.processEvents()
        QTimer.singleShot(300, lambda: (dialog.grab().save(str(out / f"{name}.png")),
                                        dialog.close()))

    steps = [
        lambda: snap("01_welcome"),
        lambda: window.set_project(project),
        lambda: window.explorer.select("asset", "v_anaconda"),
        lambda: snap("02_asset"),
        lambda: window.explorer.select("asset", "v_anaconda_hands"),
        lambda: snap("03_asset_hands"),
        lambda: window.inspector.setCurrentWidget(window.inspector.bones_page),
        lambda: snap("04_bones"),
        lambda: window.explorer.select("build", "view"),
        lambda: snap("05_build"),
        lambda: grab_dialog(DeriveDialog(["v_anaconda"], window, existing=set(project.assets),
                                         kinds={"v"}), "06_retarget"),
        lambda: grab_dialog(NewBuildDialog(project, window), "07_new_build"),
        lambda: QTimer.singleShot(800, app.quit),  # after the last dialog grab
    ]
    window.show()
    for index, step in enumerate(steps):
        QTimer.singleShot(800 + index * 500, step)
    app.exec()
    shutil.rmtree(work, ignore_errors=True)
    print(f"screenshots -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(Path(sys.argv[1]) if len(sys.argv) > 1 else Path("tmp/ui")))
