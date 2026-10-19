@echo off
chcp 65001 >nul
title BOSS Dashboard
cd /d "%~dp0"

rem ============================================================
rem  This file is intentionally ASCII-only, CRLF line endings.
rem  Chinese comments in a .bat break under the default GBK code
rem  page: cmd mis-parses them and runs fragments as commands
rem  ("'m' is not recognized as an internal or external command").
rem
rem  AI model / API key are now configured in the web page:
rem    open dashboard -> click Settings -> fill endpoint/key/model -> Save
rem  Stored in settings.json next to this script. No file editing needed.
rem ============================================================

rem  Optional legacy fallback: load secrets.env if present.
rem  Priority: settings.json > env vars > secrets.env
if exist "secrets.env" (
  for /f "usebackq eol=# tokens=1,* delims==" %%a in ("secrets.env") do (
    if not "%%a"=="" set "%%a=%%b"
  )
)

echo.
echo   BOSS Dashboard  -^>  http://127.0.0.1:8765
echo   AI config: click Settings in the page (no config file needed)
echo   Close this window to stop the server.
echo.

start "" http://127.0.0.1:8765

where py >nul 2>nul
if %errorlevel%==0 (
  py server.py
) else (
  python server.py
)

echo.
echo   Server stopped. Press any key to close.
pause >nul
