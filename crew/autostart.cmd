@echo off
rem Runs the crew unattended: for the Windows Startup folder, or a manual double-click.
rem Sets CR_UNATTENDED=1 so "Start Common Room Crew.cmd" skips its interactive prompts and
rem its final pause, and exits with a nonzero code on failure instead of waiting for a
rem keypress. See crew\autostart-install.cmd (which adds a one-line Startup entry that
rem calls this file) and INTEGRATION_NOTES.md (the exact CR_UNATTENDED lines the launcher
rem needs). This file only sets that variable and starts the launcher minimized; it never
rem touches the Startup folder itself.
setlocal
set "CR_UNATTENDED=1"
rem cmd /c (not a bare batch path): the window closes when the launcher finishes.
start "Common Room autostart" /min cmd /c ""%~dp0..\Start Common Room Crew.cmd""
