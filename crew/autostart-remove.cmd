@echo off
rem Removes the ONE file crew\autostart-install.cmd adds to the current user's Startup
rem folder. Asks for a typed "yes" first. Touches nothing else; never calls schtasks.
rem
rem Every path ends at :finish, which pauses so a double-clicked window stays readable.
rem No ( ) blocks: a ")" in a folder name would end one early.
setlocal
set "STARTUP=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup"
set "TARGET=%STARTUP%\Common Room Crew.cmd"
set "RC=1"
if not exist "%TARGET%" goto nothing
echo This will delete the Startup-folder entry that starts the Common Room crew
echo automatically at sign-in:
echo   %TARGET%
echo Nothing else on this computer is changed.
rem Cleared first: set /p keeps an existing value when Enter is pressed on an empty line,
rem so a CONFIRM=yes already in the environment must never count as the answer.
set "CONFIRM="
set /p "CONFIRM=Type yes to continue, anything else to cancel: "
if /i not "%CONFIRM%"=="yes" goto cancelled
del "%TARGET%"
if exist "%TARGET%" goto notremoved
echo Done. Removed:
echo   %TARGET%
echo The Common Room crew will no longer start automatically at sign-in.
echo To add it back, run crew\autostart-install.cmd from this tree.
set "RC=0"
goto finish
:nothing
echo No Startup entry found at:
echo   %TARGET%
echo Nothing to remove.
set "RC=0"
goto finish
:cancelled
echo Cancelled; nothing was removed.
goto finish
:notremoved
echo Could not remove the file. Delete it by hand:
echo   %TARGET%
goto finish
:finish
pause
exit /b %RC%
