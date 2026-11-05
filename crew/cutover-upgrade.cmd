@echo off
rem Shane's one switch-over command for the Common Room upgrade. Run it from the integration tree (tree-next),
rem never from the live tree it switches: it stops the crew, backs up, switches the live branch to the upgrade,
rem runs the full test suite and, if anything fails, puts everything back and starts the old crew.
rem   crew\cutover-upgrade.cmd --dry-run        rehearse on a scratch copy (touches nothing live)
rem   crew\cutover-upgrade.cmd                  the real switch (asks you to type YES)
rem   crew\cutover-upgrade.cmd --rollback <folder>   the manual undo from a backup folder
rem The work is done by cutover_upgrade.py beside this file. The last line chains the exit on the same line so cmd never re-reads
rem this file after the helper returns, even if a rollback rewrites it.
call "%~dp0find-python.cmd" || exit /b 1
"%PY%" -X utf8 -B "%~dp0cutover_upgrade.py" %* & call exit /b %%errorlevel%%
