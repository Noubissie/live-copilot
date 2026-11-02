@echo off
cd /d "%~dp0"
if not exist .venv (
  python -m venv .venv
  .venv\Scripts\pip install -r requirements.txt
)
if not exist .env (
  copy .env.example .env
  echo.
  echo Created .env - open it, add your TikTok username and Anthropic API key, then run this again.
  notepad .env
  pause
  exit /b
)
start "" http://localhost:8765/panel
.venv\Scripts\python app.py %*
pause
