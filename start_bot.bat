@echo off
rem Запуск бота двойным щелчком. Окно не закрывать, пока бот нужен.
chcp 65001 > nul
cd /d "%~dp0"
title Queue bot
.venv\Scripts\python.exe main.py
echo.
echo Бот остановлен. Если выше есть ошибка — скопируй её.
pause
