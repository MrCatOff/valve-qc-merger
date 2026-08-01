#!/bin/sh
# Build a standalone valve-qc-merger executable (macOS/Linux).
#
# PyInstaller does NOT cross-compile: run this ON the OS you are building
# for. For a Windows .exe use tools/build_exe.bat on Windows, or let the
# GitHub Actions workflow (.github/workflows/build-exe.yml) build it.
#
# Usage:  sh tools/build_exe.sh          # from the repo root
# Output: dist/valve-qc-merger
set -e
cd "$(dirname "$0")/.."

python3 -m pip install --quiet pyinstaller .
python3 -m PyInstaller \
    --onefile \
    --name valve-qc-merger \
    --add-data "storage/hands/reference_hands.smd:storage/hands" \
    --add-data "src/valve_qc_merger/retarget/worker.py:valve_qc_merger/retarget" \
    --distpath dist \
    --workpath build/pyinstaller \
    --noconfirm \
    tools/exe_entry.py

echo
echo "Built: dist/valve-qc-merger"
dist/valve-qc-merger --version
