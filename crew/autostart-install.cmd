@echo off
rem Adds ONE file to the current user's Startup folder so the Common Room crew starts
rem automatically at sign-in. Asks for a typed "yes" first. Refuses if the file already
rem exists (run crew\autostart-remove.cmd first if you want to replace it).
rem
rem A Startup-folder entry runs as the signed-in user with no elevation and needs no
rem administrator rights, unlike a Windows Scheduled Task set to run ONLOGON (INFERENCE:
rem this build never ran schtasks to confirm it; nothing here calls schtasks either way).
rem
rem Every path ends at :finish, which pauses so a double-clicked window stays readable.
rem No ( ) blocks: a ")" in a folder name would end one early.
setlocal
set "TREE=%~dp0.."
for %%I in ("%TREE%") do set "TREE=%%~fI"
set "STARTUP=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup"
set "TARGET=%STARTUP%\Common Room Crew.cmd"
set "RC=1"
if exist "%TARGET%" goto already
echo This will add one file to your Windows Startup folder:
echo   %TARGET%
echo It will contain exactly one line, starting this tree's crew\autostart.cmd minimized:
echo   start "" /min cmd /c ""%TREE%\crew\autostart.cmd""
echo Nothing else on this computer is changed. No Windows Scheduled Task is created.
rem Cleared first: set /p keeps an existing value when Enter is pressed on an empty line,
rem so a CONFIRM=yes already in the environment must never count as the answer.
set "CONFIRM="
set /p "CONFIRM=Type yes to continue, anything else to cancel: "
if /i not "%CONFIRM%"=="yes" goto cancelled
rem cmd /c, not a bare batch path: a batch started directly keeps its window open afterwards.
(echo start "" /min cmd /c ""%TREE%\crew\autostart.cmd"")>"%TARGET%"
if not exist "%TARGET%" goto notwritten
echo Done. Wrote:
echo   %TARGET%
echo The Common Room crew will now start automatically the next time you sign in to Windows.
echo To undo this, run crew\autostart-remove.cmd from this tree.
set "RC=0"
goto finish
:already
echo A Startup entry already exists at:
echo   %TARGET%
echo Nothing was changed. Run crew\autostart-remove.cmd first if you want to replace it.
goto finish
:cancelled
echo Cancelled; nothing was written.
goto finish
:notwritten
echo Could not write the Startup entry. Nothing was changed.
goto finish
:finish
pause
exit /b %RC%
