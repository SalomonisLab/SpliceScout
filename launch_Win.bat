@echo off
setlocal enableextensions
title GEO RNA-seq Pipeline
cd /d "%~dp0"

echo ============================================================
echo   GEO RNA-seq Pipeline - launcher (Windows)
echo   First run installs Python + packages automatically.
echo ============================================================
echo.

REM ---- 1) Find a working Python (prefer the 'py' launcher; never a Store stub) ----
set "PY="
where py >nul 2>&1 && set "PY=py"
if not defined PY ( where python >nul 2>&1 && set "PY=python" )
if defined PY (
  %PY% -c "import sys" >nul 2>&1
  if errorlevel 1 set "PY="
)
if defined PY goto have_python

REM ---- Python missing: install the LATEST version available via winget ----
echo Python 3 was not found. Installing the latest version via winget...
where winget >nul 2>&1
if errorlevel 1 goto no_winget
powershell -NoProfile -ExecutionPolicy Bypass -Command "$out=(winget search --id Python.Python --source winget --accept-source-agreements | Out-String); $m=[regex]::Matches($out,'Python\.Python\.(\d+\.\d+)') | Sort-Object { [version]$_.Groups[1].Value } | Select-Object -Last 1; $id=if($m){$m.Value}else{'Python.Python.3.13'}; Write-Host ('Installing latest Python: ' + $id); winget install -e --id $id --accept-source-agreements --accept-package-agreements"
echo.
echo Python was installed. Please CLOSE this window and double-click launch_Win.bat again
echo so Windows can pick up the updated PATH.
echo.
pause
exit /b 0

:no_winget
echo.
echo winget is unavailable. Please install the latest Python 3 from:
echo     https://www.python.org/downloads/
echo IMPORTANT: tick "Add python.exe to PATH" during setup, then run launch_Win.bat again.
echo.
pause
exit /b 1

:have_python
echo Using Python: %PY%
echo.

REM ---- 2) Ensure required packages (installs only when something is missing) ----
%PY% -c "import anthropic, openai, openpyxl" >nul 2>&1
if errorlevel 1 (
  echo Installing required packages ^(first run only, needs internet^)...
  %PY% -m ensurepip >nul 2>&1
  %PY% -m pip install --upgrade pip >nul 2>&1
  %PY% -m pip install -r requirements.txt
  if errorlevel 1 (
    echo.
    echo Package install failed - check your internet connection and try again.
    echo.
    pause
    exit /b 1
  )
  echo Done installing.
  echo.
)

REM ---- 3) Optional: paramiko enables SSH *password* auth for the cluster upload ----
REM      (SSH key / agent auth works fine without it, so this never blocks launch)
%PY% -c "import paramiko" >nul 2>&1
if errorlevel 1 (
  echo Installing optional package 'paramiko' ^(SSH password auth^)...
  %PY% -m pip install paramiko >nul 2>&1
)

REM ---- 3b) Vendored Plotly for the Plots tab (download once if missing) ----
if not exist "%~dp0vendor\plotly.min.js" (
  echo Downloading the Plotly charting library ^(first run only, for the Plots tab^)...
  if not exist "%~dp0vendor" mkdir "%~dp0vendor"
  powershell -NoProfile -ExecutionPolicy Bypass -Command "try{ [Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12; Invoke-WebRequest -Uri 'https://cdn.plot.ly/plotly-2.35.2.min.js' -OutFile '%~dp0vendor\plotly.min.js' }catch{ Write-Host 'Plotly download failed - the Plots tab will need internet once.' }"
)

REM ---- 3b2) Decompress vendored reference DB(s) shipped gzip'd (first run only, ~18 MB -> ~100 MB) ----
%PY% ensure_refs.py

REM ---- 3c) Optional: tray libraries (pystray + Pillow) for the system-tray launcher ----
%PY% -c "import pystray, PIL" >nul 2>&1
if errorlevel 1 (
  echo Installing tray support ^(pystray, Pillow^) for the system-tray icon...
  %PY% -m pip install pystray Pillow >nul 2>&1
)

REM ---- 3d) Name this instance -> its cluster JOB_TAG (namespaces this project's LSF jobs) ----
REM      Asked here (this window has a console) because the tray launch below is windowless and
REM      cannot prompt. Passed down via the environment variable SPLICESCOUT_INSTANCE.
set "SPLICESCOUT_INSTANCE="
set /p "SPLICESCOUT_INSTANCE=Name this instance (e.g. A549, MDAMB231) [blank = auto sra1/sra2/...]: "
if defined SPLICESCOUT_INSTANCE (
  echo Using instance name "%SPLICESCOUT_INSTANCE%" as the cluster JOB_TAG.
) else (
  echo No name given - this instance will auto-pick the next free tag ^(sra1, sra2, ...^).
)
echo.

REM ---- 4) Launch: prefer the system tray (no console window); fall back to a console window ----
set "PYW=pythonw"
if /I "%PY%"=="py" set "PYW=pyw"
%PY% -c "import pystray, PIL" >nul 2>&1
if errorlevel 1 goto run_console
goto run_tray

:run_console
echo Starting the web UI in this window ^(tray libraries unavailable^).
echo Keep this window open while you work; close it ^(or press Ctrl+C^) to stop.
echo TIP: run launch_Win.bat again for ANOTHER instance ^(it prompts for its own name + own port^).
echo.
%PY% server.py
if errorlevel 1 (
  echo.
  echo The server exited with an error - see the messages above.
)
echo.
pause
goto :eof

:run_tray
echo Starting SpliceScout in the system tray - look for the blue "S" icon near the clock.
echo Right-click it for Open / Quit. You can close this window. Run launch_Win.bat again for
echo another concurrent instance ^(it prompts for its own name + own port^).
REM The tray runs windowless (pythonw); under pythonw the app's own browser auto-open can silently
REM no-op, so the app writes its URL to _last_url.txt and WE open the browser from here (a reliable
REM console context). SPLICESCOUT_OPENED_BY_LAUNCHER tells tray.py not to also open it (avoids 2 tabs).
del "%~dp0_last_url.txt" >nul 2>&1
set "SPLICESCOUT_OPENED_BY_LAUNCHER=1"
start "SpliceScout" %PYW% "%~dp0tray.py"
echo Waiting for the server to come up, then opening it in your browser...
set "SS_URL="
for /L %%i in (1,1,30) do if not defined SS_URL (
  if exist "%~dp0_last_url.txt" set /p SS_URL=<"%~dp0_last_url.txt"
  if not defined SS_URL ping -n 2 127.0.0.1 >nul
)
if defined SS_URL (
  echo SpliceScout is running at %SS_URL%
  start "" "%SS_URL%"
) else (
  echo Could not auto-detect the URL; right-click the tray "S" icon near the clock and choose Open.
)
timeout /t 3 >nul
goto :eof
