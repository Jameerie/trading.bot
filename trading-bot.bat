@echo off
REM ===========================================================================
REM   trading.bot - one file that does the whole thing, on Windows.
REM
REM   Double-click it, or from a terminal:   trading-bot.bat
REM
REM   It finds Python, fetches the code if you do not have it, installs it into
REM   an isolated environment, makes you an access token, then starts the app
REM   and opens your browser. Run it again any time to restart.
REM
REM   Options:
REM     --port N       serve on a different port (default 8787)
REM     --dir PATH     install somewhere other than %USERPROFILE%\trading.bot
REM     --scan         print what to do right now, then exit (no server)
REM     --test         run the full test suite before starting
REM     --no-open      do not open a browser
REM     --local-only   bind to this machine only; no phone access
REM     --update       pull the latest code first
REM     --help         show this
REM
REM   This tool tells you what to do. It cannot place a trade, and it never will.
REM ===========================================================================

setlocal
title trading.bot

set "REPO_URL=https://github.com/Jameerie/trading.bot"
set "INSTALL_DIR=%USERPROFILE%\trading.bot"
set "PORT=8787"
set "BIND=0.0.0.0"
set "DO_OPEN=1"
set "DO_TEST=0"
set "DO_SCAN=0"
set "DO_UPDATE=0"
set "DIR_EXPLICIT=0"
set "PYTHON="

REM ------------------------------------------------------------------ flags
:parse
if "%~1"=="" goto parsed
if /i "%~1"=="--help"       goto usage
if /i "%~1"=="-h"           goto usage
if /i "%~1"=="--port"       goto set_port
if /i "%~1"=="--dir"        goto set_dir
if /i "%~1"=="--scan"       goto set_scan
if /i "%~1"=="--test"       goto set_test
if /i "%~1"=="--no-open"    goto set_noopen
if /i "%~1"=="--local-only" goto set_local
if /i "%~1"=="--update"     goto set_update
echo.
echo Stopped: unknown option "%~1"  (try --help)
echo.
goto fail

:set_port
set "PORT=%~2"
shift
shift
goto parse
:set_dir
set "INSTALL_DIR=%~2"
set "DIR_EXPLICIT=1"
shift
shift
goto parse
:set_scan
set "DO_SCAN=1"
set "DO_OPEN=0"
shift
goto parse
:set_test
set "DO_TEST=1"
shift
goto parse
:set_noopen
set "DO_OPEN=0"
shift
goto parse
:set_local
set "BIND=127.0.0.1"
shift
goto parse
:set_update
set "DO_UPDATE=1"
shift
goto parse

:usage
echo.
echo   trading.bot - fetches, installs and starts the forex advisor.
echo.
echo   Usage:  trading-bot.bat [options]
echo.
echo     --port N       serve on a different port (default 8787)
echo     --dir PATH     install somewhere other than %%USERPROFILE%%\trading.bot
echo     --scan         print what to do right now, then exit (no server)
echo     --test         run the full test suite before starting
echo     --no-open      do not open a browser
echo     --local-only   bind to this machine only; no phone access
echo     --update       pull the latest code first
echo     --help         show this
echo.
echo   It tells you what to do. It cannot place a trade, and it never will.
echo.
goto done

:parsed
echo.
echo   trading.bot   it tells you what to do; you place the trade
echo.

REM ------------------------------------------------------------- 1. Python
echo 1/5  Checking what this machine has

call :try_python "py -3.14"
call :try_python "py -3.13"
call :try_python "py -3.12"
call :try_python "py -3.11"
call :try_python "py -3"
call :try_python "python3"
call :try_python "python"
if not defined PYTHON goto no_python

for /f "delims=" %%v in ('%PYTHON% --version 2^>^&1') do set "PYVER=%%v"
echo   ok  %PYVER%

REM --------------------------------------------------------------- 2. Code
echo.
echo 2/5  Getting the code

if "%DIR_EXPLICIT%"=="1" goto check_existing
if not exist "pyproject.toml" goto check_existing
if not exist "src\trading_bot" goto check_existing
set "INSTALL_DIR=%CD%"
echo   ok  using the checkout you are already in: %INSTALL_DIR%
goto have_code

:check_existing
if not exist "%INSTALL_DIR%\src\trading_bot" goto clone
if "%DO_UPDATE%"=="1" goto pull
echo   ok  found an existing install at %INSTALL_DIR%
goto have_code

:pull
where git >nul 2>&1
if errorlevel 1 goto no_update
git -C "%INSTALL_DIR%" pull --ff-only >nul 2>&1
if errorlevel 1 goto no_update
echo   ok  updated %INSTALL_DIR%
goto have_code
:no_update
echo   !!  could not update; using the code that is there
goto have_code

:clone
where git >nul 2>&1
if errorlevel 1 goto no_git
echo   ..  cloning into %INSTALL_DIR%
git clone --depth 1 "%REPO_URL%" "%INSTALL_DIR%" >nul 2>&1
if errorlevel 1 goto clone_failed
echo   ok  cloned to %INSTALL_DIR%

:have_code
cd /d "%INSTALL_DIR%"
if errorlevel 1 goto bad_dir

REM ------------------------------------------------------------ 3. Install
echo.
echo 3/5  Installing into an isolated environment

set "FIRST_RUN=0"
if exist ".venv\Scripts\python.exe" goto have_venv
set "FIRST_RUN=1"
%PYTHON% -m venv ".venv" >nul 2>&1
if errorlevel 1 goto no_venv

:have_venv
set "VPY=.venv\Scripts\python.exe"
if not exist "%VPY%" goto broken_venv

if "%FIRST_RUN%"=="1" goto do_install
if "%DO_UPDATE%"=="1" goto do_install
echo   ok  already installed (pass --update to refresh)
goto installed

:do_install
"%VPY%" -m pip install --upgrade pip >nul 2>&1
REM The app itself has no dependencies, so this works with no network at all.
REM Only pytest needs one, and losing it costs nothing but the --test flag.
"%VPY%" -m pip install -e ".[dev]" >nul 2>&1
if errorlevel 1 goto install_core
echo   ok  installed, with the test suite
goto installed

:install_core
"%VPY%" -m pip install -e . >nul 2>&1
if errorlevel 1 goto install_failed
echo   !!  installed; pytest unavailable (offline?) so --test will not work

:installed

REM -------------------------------------------------------------- 4. Setup
echo.
echo 4/5  Preparing your configuration

if not exist "reports" mkdir "reports"
if exist ".env" goto have_env

for /f "delims=" %%t in ('"%VPY%" -c "import secrets;print(secrets.token_urlsafe(24))"') do set "NEWTOKEN=%%t"
> ".env" echo # Access token for the web app. Anyone with this can read your signals
>> ".env" echo # and write to your journal, so treat it like a password. Delete this
>> ".env" echo # file and run this again to issue a new one.
>> ".env" echo TRADING_BOT_TOKEN=%NEWTOKEN%
>> ".env" echo.
>> ".env" echo # Optional Twelve Data key for live prices. Without it the app reads
>> ".env" echo # the synthetic CSV files in data/samples/.
>> ".env" echo # TRADING_BOT_API_KEY=
echo   ok  generated a fresh access token
goto read_env

:have_env
echo   ok  keeping the access token already in .env

:read_env
set "TRADING_BOT_TOKEN="
for /f "tokens=1,* delims==" %%a in ('findstr /b "TRADING_BOT_TOKEN=" ".env"') do set "TRADING_BOT_TOKEN=%%b"

REM ------------------------------------------------------------- 5. Verify
echo.
echo 5/5  Checking it actually works

"%VPY%" -c "import trading_bot" >nul 2>&1
if errorlevel 1 goto import_failed
echo   ok  the package imports cleanly

if not "%DO_TEST%"=="1" goto verified
echo   ..  running the full test suite
REM Run with the credentials cleared. The suite is hermetic about this now, but
REM an older checkout is not, and a token in .env would turn it red.
set "SAVED_TOKEN=%TRADING_BOT_TOKEN%"
set "TRADING_BOT_TOKEN="
set "TRADING_BOT_API_KEY="
"%VPY%" -m pytest -q >nul 2>&1
if errorlevel 1 goto tests_failed
set "TRADING_BOT_TOKEN=%SAVED_TOKEN%"
echo   ok  all tests pass

:verified

REM ---------------------------------------------------------------- Scan
if not "%DO_SCAN%"=="1" goto serve
echo.
"%VPY%" -m trading_bot --config config/default.toml scan
goto done

REM ---------------------------------------------------------------- Serve
:serve
"%VPY%" -c "import socket,sys;s=socket.socket();r=s.connect_ex(('127.0.0.1',int(sys.argv[1])));s.close();sys.exit(0 if r else 1)" %PORT%
if errorlevel 1 goto port_busy

if "%FIRST_RUN%"=="1" call :first_run_note
if "%DO_OPEN%"=="1" start "" /b "%VPY%" -c "import time,webbrowser,sys;time.sleep(4);webbrowser.open(sys.argv[1])" "http://localhost:%PORT%/?token=%TRADING_BOT_TOKEN%"

echo.
echo Starting. Close this window or press Ctrl-C to stop.
"%VPY%" -m trading_bot --config config/default.toml serve --host %BIND% --port %PORT%
goto done

REM ----------------------------------------------------------- subroutines
:try_python
if defined PYTHON exit /b 0
%~1 -c "import sys; sys.exit(0 if sys.version_info>=(3,11) else 1)" >nul 2>&1
if errorlevel 1 exit /b 0
set "PYTHON=%~1"
exit /b 0

:first_run_note
echo.
echo   Before you trade on any of this: the bundled data in data\samples\ is
echo   synthetic, and the strategy has no demonstrated edge on it. Load your
echo   own broker history and backtest with --split 0.7 before believing a
echo   number. SETUP.md explains how.
exit /b 0

REM --------------------------------------------------------------- errors
:no_python
echo.
echo Stopped: no Python 3.11 or newer was found.
echo.
echo   Install it from https://www.python.org/downloads/
echo   On the first screen, tick "Add python.exe to PATH".
echo   Then run this file again.
echo.
goto fail

:no_git
echo.
echo Stopped: git is not installed, so the code cannot be fetched.
echo.
echo   Install it from https://git-scm.com/download/win
echo   Or download the ZIP from %REPO_URL%
echo   unzip it, and run this file from inside the folder.
echo.
goto fail

:clone_failed
echo.
echo Stopped: could not download the code. Check your internet connection,
echo   or download the ZIP from %REPO_URL% and run this file inside it.
echo.
goto fail

:bad_dir
echo.
echo Stopped: could not enter %INSTALL_DIR%
echo.
goto fail

:no_venv
echo.
echo Stopped: could not create the environment. Your Python install may be
echo   incomplete - reinstall it from https://www.python.org/downloads/
echo.
goto fail

:broken_venv
echo.
echo Stopped: the environment in %INSTALL_DIR%\.venv looks broken.
echo   Delete that folder and run this file again.
echo.
goto fail

:install_failed
echo.
echo Stopped: the install failed. Run this to see why:
echo   cd /d "%INSTALL_DIR%"
echo   .venv\Scripts\python.exe -m pip install -e ".[dev]"
echo.
goto fail

:import_failed
echo.
echo Stopped: the package will not import. Something is wrong with the install.
echo   Delete %INSTALL_DIR%\.venv and run this file again.
echo.
goto fail

:tests_failed
echo.
echo Stopped: the test suite failed. See it with:
echo   cd /d "%INSTALL_DIR%"
echo   .venv\Scripts\python.exe -m pytest
echo.
goto fail

:port_busy
echo.
echo Stopped: port %PORT% is already in use - trading.bot may already be running.
echo   Open http://localhost:%PORT%/ , or start this one elsewhere:
echo       trading-bot.bat --port 8788
echo.
goto fail

:fail
if "%DO_SCAN%"=="1" exit /b 1
pause
exit /b 1

:done
if "%DO_SCAN%"=="1" exit /b 0
pause
exit /b 0
