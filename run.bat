@echo off
chcp 65001 >nul
REM Прогон одной командой на Windows: run.bat docs\requirements.pdf
if "%~1"=="" (set IN=docs\requirements.pdf) else (set IN=%~1)
python -m validator "%IN%" -o reports\report.md --gold gold\defects.md
