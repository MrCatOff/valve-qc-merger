#!/bin/sh
# Build the valve-qc-studio GUI (macOS/Linux). PyInstaller does NOT
# cross-compile: build ON the OS you target; Windows uses
# tools/build_studio.bat (or the GitHub Actions workflow).
#
# Usage:  sh tools/build_studio.sh          # from the repo root
# Output: dist/valve-qc-studio/ (+ dist/valve-qc-studio.app on macOS)
set -e
cd "$(dirname "$0")/.."

python3 -m pip install --quiet pyinstaller ".[studio]"
python3 -m PyInstaller tools/studio.spec --noconfirm \
    --distpath dist --workpath build/studio

echo
echo "Built: dist/valve-qc-studio"
dist/valve-qc-studio/valve-qc-studio --selftest tests/examples/mdl/mini.mdl
