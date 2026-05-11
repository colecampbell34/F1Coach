@echo off
setlocal
cd /d "%~dp0"

where py >nul 2>nul
if %errorlevel%==0 (
  py -3 -m f1coach dashboard --open-browser --show-packets
  goto :done
)

where python >nul 2>nul
if %errorlevel%==0 (
  python -m f1coach dashboard --open-browser --show-packets
  goto :done
)

echo Python 3.10 or newer is required.
echo Download Python from https://www.python.org/downloads/
pause
exit /b 1

:done
pause
