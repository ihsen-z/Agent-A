@echo off
rem Double-cliquer sur ce fichier pour ouvrir l'interface dans le navigateur.
chcp 65001 >NUL
cd /d "%~dp0"
where py >NUL 2>NUL
if %errorlevel%==0 (
    py -3 scripts\interface.py %*
) else (
    python scripts\interface.py %*
)
if errorlevel 1 (
    echo.
    echo Python 3.11 ou plus recent est necessaire : https://www.python.org/downloads/
    pause
)
