# PyInstaller spec for valve-qc-studio (the GUI). Build from the repo root:
#     python -m PyInstaller tools/studio.spec --noconfirm --distpath dist --workpath build/studio
# Output: dist/valve-qc-studio/ (a folder; Windows: valve-qc-studio.exe inside,
# macOS: also dist/valve-qc-studio.app). One-folder, not one-file: a one-file
# build would unpack ~150 MB of Qt on every start.
import os
import sys

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))  # noqa: F821 - set by PyInstaller

# Data the services read at run time (resources.data_root() == the bundle).
datas = [
    (os.path.join(ROOT, "storage", "hands"), os.path.join("storage", "hands")),
    (os.path.join(ROOT, "storage", "handswap"), os.path.join("storage", "handswap")),
    (os.path.join(ROOT, "storage", "players_donor"), os.path.join("storage", "players_donor")),
    (os.path.join(ROOT, "storage", "zhands"), os.path.join("storage", "zhands")),
    (os.path.join(ROOT, "storage", "server"), os.path.join("storage", "server")),
    (os.path.join(ROOT, "src", "valve_qc_merger", "studio", "icons"),
     os.path.join("valve_qc_merger", "studio", "icons")),
    (os.path.join(ROOT, "src", "valve_qc_merger", "retarget", "worker.py"),
     os.path.join("valve_qc_merger", "retarget")),
]

# Qt modules the studio never imports (keeps the folder far smaller).
excludes = [
    "tkinter", "PySide6.QtQml", "PySide6.QtQuick", "PySide6.QtQuickWidgets",
    "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.QtWebChannel",
    "PySide6.QtMultimedia", "PySide6.QtMultimediaWidgets", "PySide6.Qt3DCore",
    "PySide6.Qt3DRender", "PySide6.QtCharts", "PySide6.QtDataVisualization",
    "PySide6.QtPdf", "PySide6.QtPdfWidgets", "PySide6.QtSql", "PySide6.QtTest",
    "PySide6.QtBluetooth", "PySide6.QtNfc", "PySide6.QtPositioning",
    "PySide6.QtSensors", "PySide6.QtSerialPort", "PySide6.QtDesigner",
    "pyvista", "pyvistaqt", "vtk", "matplotlib", "IPython", "pytest",
]

a = Analysis(  # noqa: F821
    [os.path.join(ROOT, "tools", "studio_entry.py")],
    pathex=[os.path.join(ROOT, "src")],
    datas=datas,
    hiddenimports=["PySide6.QtOpenGL", "PySide6.QtOpenGLWidgets", "PySide6.QtSvg"],
    excludes=excludes,
    noarchive=False,
)
pyz = PYZ(a.pure)  # noqa: F821
exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="valve-qc-studio",
    icon=os.path.join(ROOT, "src", "valve_qc_merger", "studio", "icons", "app", "app.ico"),
    console=False,  # a windowed app: no console window on Windows
    disable_windowed_traceback=False,
    upx=False,
)
coll = COLLECT(  # noqa: F821
    exe, a.binaries, a.datas, strip=False, upx=False, name="valve-qc-studio",
)
if sys.platform == "darwin":
    app = BUNDLE(  # noqa: F821
        coll,
        name="valve-qc-studio.app",
        bundle_identifier="dev.valve-qc-merger.studio",
        icon=os.path.join(ROOT, "src", "valve_qc_merger", "studio", "icons", "app",
                          "app.icns"),
        info_plist={"NSHighResolutionCapable": True},
    )
