@echo off
rem Builds dist\ClaudeUsageWidget.exe (single file, no console window).
python -m venv .venv || exit /b 1
.venv\Scripts\python -m pip install -q pyinstaller || exit /b 1
.venv\Scripts\pyinstaller --onefile --noconsole --name ClaudeUsageWidget --clean -y widget.pyw
