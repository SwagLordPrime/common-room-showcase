@echo off
rem One trial cycle of a headless agent runner seat (see AUTONOMY_2026-09-24.md), then stops.
rem Usage, from anywhere:  "D:\Work Area\Artifacts\common-room\tree\crew\agent-once.cmd" claude-code
setlocal
cd /d "%~dp0.."
if "%~1"=="" goto usage
if not exist "crew\agent-%~1.json" goto noconfig
call "%~dp0find-python.cmd" || exit /b 1
"%PY%" -X utf8 -B -m common_room.agent_worker --config "crew\agent-%~1.json" --once
set "RC=%errorlevel%"
echo.
echo Exit code %RC%: 0 worked, asked or idle; 2 deferred (cap, command missing, seat busy); 1 error.
echo Status: .runtime\handoffs\workers\%~1-status.json
exit /b %RC%
:usage
echo Usage: crew\agent-once.cmd SEAT   (for example: crew\agent-once.cmd claude-code)
exit /b 2
:noconfig
echo There is no crew\agent-%~1.json for that seat.
exit /b 2
