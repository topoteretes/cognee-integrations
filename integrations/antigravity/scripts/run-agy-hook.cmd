@echo off
rem Select the interpreter before starting the adapter. Do not retry a hook
rem after it has begun: prompt/tool capture can make durable state changes.
setlocal EnableExtensions
where python3 >nul 2>nul
if errorlevel 1 goto use_python
python3 "%~dp0agy_hook.py" %*
exit /b %errorlevel%

:use_python
python "%~dp0agy_hook.py" %*
exit /b %errorlevel%
