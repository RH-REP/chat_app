@echo off
rem Start chat_app on Windows (double-click). Needs Python 3.8+ from python.org or the Microsoft Store.
cd /d "%~dp0"
where py >nul 2>nul
if %errorlevel%==0 (
  py -3 chat.py %*
) else (
  python chat.py %*
)
echo.
pause
