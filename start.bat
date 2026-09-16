@echo off
REM ==============================================================
REM  Dev startup script (native Windows, no Git Bash / WSL needed).
REM  Starts the FastAPI backend and the React (GOLDSIGNAL) frontend
REM  together, in PAPER/MOCK mode, and opens the dashboard in your
REM  browser.
REM
REM  Usage (double-click, or run from a Command Prompt):
REM    start.bat                 backend (:8000) + frontend (:5173)
REM    start.bat --with-loop     also runs the trading loop
REM                               continuously in the background, so
REM                               PAPER positions actually open/close
REM                               and dashboard-initiated closes get
REM                               processed
REM    start.bat --backend-only  just the API
REM    start.bat --frontend-only just the frontend dev server
REM
REM  SAFETY: this refuses to start if .env asks for anything other
REM  than TRADING_MODE=PAPER (or BACKTEST) with MT5_USE_MOCK=true. It
REM  does not silently rewrite your configuration to make it "work".
REM ==============================================================

setlocal enabledelayedexpansion
cd /d "%~dp0"

set "WITH_LOOP=0"
set "RUN_BACKEND=1"
set "RUN_FRONTEND=1"

:parse_args
if "%~1"=="" goto args_done
if /I "%~1"=="--with-loop" set "WITH_LOOP=1"
if /I "%~1"=="--backend-only" set "RUN_FRONTEND=0"
if /I "%~1"=="--frontend-only" set "RUN_BACKEND=0"
shift
goto parse_args
:args_done

REM --------------------------------------------------------------
REM 1. Safety gate -- refuse anything but PAPER/BACKTEST + mock MT5.
REM --------------------------------------------------------------
set "ENV_TRADING_MODE=PAPER"
set "ENV_MT5_MOCK=true"
if exist .env (
  for /f "usebackq tokens=1,* delims==" %%A in (`findstr /R /C:"^TRADING_MODE=" .env`) do set "ENV_TRADING_MODE=%%B"
  for /f "usebackq tokens=1,* delims==" %%A in (`findstr /R /C:"^MT5_USE_MOCK=" .env`) do set "ENV_MT5_MOCK=%%B"
)

set "SAFE_MODE=1"
if /I not "%ENV_TRADING_MODE%"=="PAPER" if /I not "%ENV_TRADING_MODE%"=="BACKTEST" set "SAFE_MODE=0"
if /I not "%ENV_MT5_MOCK%"=="true" set "SAFE_MODE=0"

if "%SAFE_MODE%"=="0" (
  echo REFUSING TO START: TRADING_MODE=%ENV_TRADING_MODE% / MT5_USE_MOCK=%ENV_MT5_MOCK%
  echo This launcher only ever starts the system with TRADING_MODE=PAPER or BACKTEST
  echo and MT5_USE_MOCK=true.
  pause
  exit /b 1
)

set "TRADING_MODE=PAPER"
set "MT5_USE_MOCK=true"
echo Safety gate passed: TRADING_MODE=PAPER, MT5_USE_MOCK=true

if not exist .env (
  echo No .env found -- copying .env.example.
  copy /Y .env.example .env >nul
)

REM --------------------------------------------------------------
REM 2. Find Python
REM --------------------------------------------------------------
where python >nul 2>nul
if errorlevel 1 (
  echo ERROR: 'python' not found on PATH.
  echo Install Python 3.11+ from https://www.python.org/downloads/ and make sure
  echo "Add python.exe to PATH" is checked during setup, then re-run this script.
  pause
  exit /b 1
)

REM --------------------------------------------------------------
REM 3. Backend venv + deps
REM --------------------------------------------------------------
if "%RUN_BACKEND%%WITH_LOOP%" NEQ "00" (
  if not exist .venv (
    echo Creating Python venv...
    python -m venv .venv
    if errorlevel 1 (
      echo ERROR: failed to create the virtual environment.
      pause
      exit /b 1
    )
  )
  echo Installing backend requirements -- skips ones already installed...
  .venv\Scripts\pip.exe install -q --upgrade pip
  .venv\Scripts\pip.exe install -q -r requirements.txt
  if errorlevel 1 (
    echo ERROR: pip install failed. See the output above.
    pause
    exit /b 1
  )
)

REM --------------------------------------------------------------
REM 4. Backend API -- runs in its own window so you can watch its
REM    logs, and closing that window stops it.
REM --------------------------------------------------------------
if "%RUN_BACKEND%"=="1" (
  echo Starting backend API on http://127.0.0.1:8000 ...
  start "XAU Backend - PAPER MOCK" cmd /k "set TRADING_MODE=PAPER&& set MT5_USE_MOCK=true&& set PYTHONPATH=.&& .venv\Scripts\python.exe scripts\run_dashboard.py --host 127.0.0.1 --port 8000"
  echo Waiting for backend to become ready...
  call :wait_for_backend
)

REM --------------------------------------------------------------
REM 5. Trading loop (optional) -- runs continuously in its own
REM    window by repeatedly invoking the existing, safety-reviewed
REM    scripts\run_paper_trading.py rather than duplicating its logic.
REM --------------------------------------------------------------
if "%WITH_LOOP%"=="1" (
  echo Starting trading loop in its own window -- flag --with-loop...
  start "XAU Trading Loop - PAPER MOCK" cmd /k "scripts\run_loop.bat"
)

REM --------------------------------------------------------------
REM 6. Frontend dev server -- also its own window.
REM --------------------------------------------------------------
if "%RUN_FRONTEND%"=="1" (
  where npm >nul 2>nul
  if errorlevel 1 (
    echo ERROR: 'npm' not found on PATH.
    echo Install Node.js LTS from https://nodejs.org/ then re-run this script.
    pause
    exit /b 1
  )
  if not exist frontend\node_modules (
    echo Installing frontend dependencies -- this can take a minute...
    pushd frontend
    call npm install
    if errorlevel 1 (
      echo ERROR: npm install failed. See the output above.
      popd
      pause
      exit /b 1
    )
    popd
  )
  echo Starting frontend dev server on http://localhost:5173 ...
  start "XAU Frontend" cmd /k "cd /d frontend && npm run dev -- --port 5173"
)

REM --------------------------------------------------------------
REM 7. Open the dashboard in your default browser.
REM --------------------------------------------------------------
timeout /t 2 >nul
if "%RUN_FRONTEND%"=="1" (
  start "" "http://localhost:5173"
) else if "%RUN_BACKEND%"=="1" (
  start "" "http://127.0.0.1:8000/docs"
)

echo.
echo ================================================================
echo  Backend:   http://127.0.0.1:8000   (docs at /docs)
if "%RUN_FRONTEND%"=="1" echo  Frontend:  http://localhost:5173
if "%WITH_LOOP%"=="1" (echo  Trading loop: running) else (echo  Trading loop: NOT running -- pass --with-loop to start it)
echo  Mode:      TRADING_MODE=PAPER  MT5_USE_MOCK=true
echo.
echo  The backend, frontend, and trading loop each run in their OWN
echo  window. Close a window (or press Ctrl+C inside it) to stop
echo  that piece. Closing this window does NOT stop the others.
echo ================================================================
pause
exit /b 0

REM --------------------------------------------------------------
REM Subroutine: poll http://127.0.0.1:8000/api/system for up to 30
REM seconds. Using "call :label" (not goto) so this cleanly returns
REM to the caller, and lives outside any parenthesized if-block
REM (goto/labels inside a (...) block behave unreliably in cmd.exe).
REM --------------------------------------------------------------
:wait_for_backend
set "READY_TRIES=0"
:wait_for_backend_loop
set /a READY_TRIES+=1
curl -s -o nul http://127.0.0.1:8000/api/system
if not errorlevel 1 (
  echo Backend is up.
  goto :eof
)
if %READY_TRIES% GEQ 30 (
  echo WARNING: backend did not respond within 30 seconds -- check its window for errors.
  goto :eof
)
timeout /t 1 >nul
goto wait_for_backend_loop
