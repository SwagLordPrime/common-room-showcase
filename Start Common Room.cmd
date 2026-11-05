@echo off
cd /d "%~dp0"
if defined COMMON_ROOM_PYTHON goto custom_python
if exist "%USERPROFILE%\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" (
  "%USERPROFILE%\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" -B -m common_room.room
  goto finished
)
python -B -m common_room.room
goto finished
:custom_python
"%COMMON_ROOM_PYTHON%" -B -m common_room.room
:finished
pause
