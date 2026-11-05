@echo off
rem The crew command line for any seat that can run a Windows shell (Claude Code, Hermes, Codex).
rem Usage: crew --credential .runtime\handoffs\<seat>-credential.json tasks
rem   (run "crew --help" for every command). Works from any folder when --credential is an absolute path.
setlocal
call "%~dp0crew\find-python.cmd" || exit /b 1
set "PYTHONPATH=%~dp0"
"%PY%" -X utf8 -B -m common_room.crew %*
exit /b %errorlevel%
