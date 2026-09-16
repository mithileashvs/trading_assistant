@echo off
REM Helper for start.bat --with-loop. Repeatedly invokes the existing,
REM safety-reviewed scripts\run_paper_trading.py (one iteration at a
REM time) rather than duplicating its logic, so PAPER positions keep
REM opening/closing and queued dashboard commands (position closes)
REM keep getting processed. Runs until this window is closed.
setlocal
cd /d "%~dp0\.."
set "TRADING_MODE=PAPER"
set "MT5_USE_MOCK=true"
set "PYTHONPATH=."

:loop
"%~dp0..\.venv\Scripts\python.exe" scripts\run_paper_trading.py --iterations 1 --sleep-seconds 0
timeout /t 5 >nul
goto loop
