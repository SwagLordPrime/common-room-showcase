@echo off
rem Runs the whole Common Room test suite on this PC and saves the output for Claude to read.
rem Usage:  crew\windows-tests.cmd ["<output file>"] [test file pattern]
rem         default output: WINDOWS_SUITE.txt in the tree; default pattern: test*.py (every test file)
rem Example: crew\windows-tests.cmd "%CD%\WINDOWS_AGENT.txt" test_agent_worker.py
rem Cutover uses "<output>" --fast 8; the integrator dry run keeps the serial full suite.
setlocal
cd /d "%~dp0.."
call "%~dp0find-python.cmd" || exit /b 1
if /i "%~1"=="--fast" goto fastfirst
if /i "%~2"=="--fast" goto fastout
set "OUT=%~1"
if "%OUT%"=="" set "OUT=%CD%\WINDOWS_SUITE.txt"
set "PAT=%~2"
if "%PAT%"=="" set "PAT=test*.py"
echo Running the Common Room tests (several minutes). Output: "%OUT%"
"%PY%" -X utf8 -B -m unittest discover -s tests -t . -p "%PAT%" > "%OUT%" 2>&1
set "RC=%errorlevel%"
echo Done, exit code %RC%. Tell Claude the tests finished.
exit /b %RC%

rem Fast usage: windows-tests.cmd "<output>" --fast [N] [pattern]
rem Or: windows-tests.cmd --fast [N] ["<output>"] [pattern]
:fastfirst
set "OUT=%~3"
set "JOBS=%~2"
goto fastrun
:fastout
set "OUT=%~1"
set "JOBS=%~3"
:fastrun
if "%OUT%"=="" set "OUT=%CD%\WINDOWS_SUITE.txt"
if "%JOBS%"=="" set "JOBS=0"
set "PAT=%~4"
if "%PAT%"=="" set "PAT=test*.py"
"%PY%" -X utf8 -B "%~dp0fast_tests.py" --output "%OUT%" --jobs "%JOBS%" --pattern "%PAT%"
set "RC=%errorlevel%"
exit /b %RC%
