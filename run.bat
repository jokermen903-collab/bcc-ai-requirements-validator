@echo off
chcp 65001 >nul
REM Прогон одной командой на Windows: run.bat [документ]   (по умолчанию docs\requirements.md)
if "%~1"=="" (set IN=docs\requirements.md) else (set IN=%~1)
python -m validator "%IN%" -o reports\report.md --gold gold\defects.md
