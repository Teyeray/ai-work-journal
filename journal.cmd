@echo off
REM ai-work-journal CLI (Windows). The logic lives in journal.py.
where py >nul 2>nul && (py -3 "%~dp0journal.py" %*) || (python "%~dp0journal.py" %*)
