@echo off
rem Build the valve-qc-studio GUI on Windows (from the repo root).
rem Needs Python 3.11+ only for the BUILD; the result runs without Python.
rem Output: dist\valve-qc-studio\valve-qc-studio.exe (ship the whole folder).
cd /d "%~dp0\.."

python -m pip install --quiet pyinstaller ".[studio]" || exit /b 1
python -m PyInstaller tools\studio.spec --noconfirm --distpath dist --workpath build\studio || exit /b 1

echo.
echo Built: dist\valve-qc-studio\valve-qc-studio.exe
rem windowed exe: no console output, the exit code tells
start /wait "" dist\valve-qc-studio\valve-qc-studio.exe --selftest tests\examples\mdl\mini.mdl
if errorlevel 1 (echo SELFTEST FAILED & exit /b 1)
echo SELFTEST OK
