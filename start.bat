@echo off
rem ============================================================
rem  muji launcher
rem  - creates a local virtual environment (on first run)
rem  - installs dependencies (on first run)
rem  - opens the web UI
rem  Close this window to stop everything.
rem ============================================================
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
  echo Python 3.10+ was not found on PATH.
  echo Install it from https://www.python.org/downloads/  ^(tick "Add python.exe to PATH"^)
  echo and run this file again.
  pause
  exit /b 1
)

rem First run: seed .env from the template so the user has one file to edit
rem (model endpoint + API key + tool opt-in/out). Never overwrites an existing .env.
if not exist ".env" (
  if exist ".env.example" (
    copy ".env.example" ".env" >nul
    echo First run: created .env from .env.example — edit it to set your model + API key.
  )
)

if not exist ".venv\Scripts\python.exe" (
  echo [1/3] First run: creating local virtual environment...
  python -m venv .venv
  if errorlevel 1 (
    echo Could not create .venv. Try manually:  python -m venv .venv
    pause
    exit /b 1
  )
)

".venv\Scripts\python.exe" -c "import fastapi, uvicorn, openai, httpx" >nul 2>nul
if errorlevel 1 (
  echo [2/3] First run: installing dependencies...
  ".venv\Scripts\python.exe" -m pip install --quiet -r requirements.txt
  if errorlevel 1 (
    echo Dependency install failed. Check your internet connection and re-run.
    pause
    exit /b 1
  )
) else (
  echo [2/3] Dependencies OK.
)

echo [3/3] Starting server...

powershell -NoProfile -Command "try { $c = New-Object Net.Sockets.TcpClient; $c.Connect('127.0.0.1', 8321); exit 1 } catch { exit 0 }" >nul 2>nul
if errorlevel 1 (
  echo Server is already running - opening the browser.
  start "" "http://127.0.0.1:8321"
  timeout /t 3 >nul
  exit /b 0
)

timeout /t 2 /nobreak >nul
start "" "http://127.0.0.1:8321"
".venv\Scripts\python.exe" server.py
echo.
echo Server stopped.
