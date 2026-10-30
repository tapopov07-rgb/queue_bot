@echo off
rem Запуск бота. Если бот упадёт — перезапустится сам через 10 секунд.
rem Чтобы остановить бота, просто закрой это окно.
chcp 65001 > nul
cd /d "%~dp0"
title Queue bot

:loop
.venv\Scripts\python.exe main.py
echo.
echo Бот остановился. Перезапуск через 10 секунд (закрой окно, чтобы не перезапускать)...
timeout /t 10 /nobreak > nul
goto loop
