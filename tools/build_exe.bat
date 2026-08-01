@echo off
rem Build a standalone valve-qc-merger.exe (run ON Windows, from the repo root).
rem Needs any Python 3.11+ installed just for the BUILD; the produced exe
rem runs without Python. Output: dist\valve-qc-merger.exe
rem (The GitHub Actions workflow .github/workflows/build-exe.yml runs this
rem same build on a Windows runner if you prefer not to build locally.)
cd /d "%~dp0\.."

python -m pip install --quiet pyinstaller . || exit /b 1
python -m PyInstaller ^
    --onefile ^
    --name valve-qc-merger ^
    --add-data "storage/hands/reference_hands.smd;storage/hands" ^
    --add-data "src/valve_qc_merger/retarget/worker.py;valve_qc_merger/retarget" ^
    --distpath dist ^
    --workpath build\pyinstaller ^
    --noconfirm ^
    tools\exe_entry.py || exit /b 1

echo.
echo Built: dist\valve-qc-merger.exe
dist\valve-qc-merger.exe --version
