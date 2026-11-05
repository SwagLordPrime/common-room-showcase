@echo off
rem Restarts ONLY the local-a worker window ("CRCREW local-a") so it re-reads crew\local-a.json
rem (model, budgets, quiet_windows). The room, the relay and every chat keep running.
rem A task local-a was generating is not lost: its lease lapses with no strike and it is retried.
rem local-b is not started (it is out of the 2026-09-30 run; the full launcher still starts it).
setlocal
cd /d "%~dp0.."
call "%~dp0find-python.cmd" || (pause & exit /b 1)
taskkill /F /FI "WINDOWTITLE eq CRCREW local-a*" /T >nul 2>nul
timeout /t 2 /nobreak >nul
start "CRCREW local-a" /min "%PY%" -X utf8 -B -m common_room.keepalive --name local-a -- -X utf8 -B -m common_room.ollama_worker --config crew\local-a.json --loop
echo local-a restarted with the current crew\local-a.json.
pause
