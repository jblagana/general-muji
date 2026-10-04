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

rem Read the configured port from .env (default 8321) so start.bat respects it.
set "PORT=8321"
for /f "usebackq delims=" %%p in (`powershell -NoProfile -Command "$m=Select-String -Path .env -Pattern 'PORT=' -ErrorAction SilentlyContinue | Select-Object -First 1; if($m){ ($m.Line -split '=',2)[1].Trim() }"`) do set "PORT=%%p"

rem First run: drop a permanent desktop shortcut (idempotent - only if missing
rem or pointing at the wrong start.bat, so a stale one self-heals on next launch).
rem Named "muji2.0" so it never collides with the original repo's "muji".
rem Uses the shell Desktop folder so OneDrive-redirected desktops still work.
powershell -NoProfile -Command "$d=[Environment]::GetFolderPath('Desktop'); $p=Join-Path $d 'muji2.0.lnk'; $t='%~dp0start.bat'; $s=(New-Object -ComObject WScript.Shell).CreateShortcut($p); if(-not (Test-Path $p) -or $s.TargetPath -ne $t){ $s.TargetPath=$t; $s.WorkingDirectory='%~dp0'; $s.Description='muji2.0 - local agent'; $s.IconLocation='%~dp0static\favicon.ico'; $s.Save(); Write-Host 'Created desktop shortcut: muji2.0.lnk' }"

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

rem tools\launch.py owns the rest: it only treats the port as "already
rem running" if OUR server answers /api/health (a foreign program on the
rem port is not us), starts server.py as a child (closing this window
rem stops it), and opens the browser on the port server.py actually
rem bound (recorded in server.port if it had to move off the configured one).
".venv\Scripts\python.exe" tools\launch.py
