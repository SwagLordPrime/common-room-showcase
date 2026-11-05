@echo off
rem Sets PY to the Python that runs the Common Room (3.12 or newer).
rem Order: COMMON_ROOM_PYTHON, the Codex-bundled Python the room already runs on, then "python" on PATH.
set "PY="
if defined COMMON_ROOM_PYTHON set "PY=%COMMON_ROOM_PYTHON%"
if defined PY goto check
set "CODEXPY=%USERPROFILE%\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
if exist "%CODEXPY%" set "PY=%CODEXPY%"
if defined PY goto check
set "PY=python"
:check
"%PY%" -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)" >nul 2>nul
if errorlevel 1 goto nopython
exit /b 0
:nopython
echo Could not find Python 3.12 or newer. Set COMMON_ROOM_PYTHON to its python.exe and try again.
exit /b 1
