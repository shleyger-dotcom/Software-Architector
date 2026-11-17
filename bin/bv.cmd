@echo off
rem Software-Architector CLI launcher for PowerShell/cmd. Prefers the py launcher: python3 is often a Store stub.
setlocal
set "AP=%~dp0..\scripts\bv.py"
where py >nul 2>nul
if %errorlevel%==0 (py -3 "%AP%" %*) else (python "%AP%" %*)
exit /b %errorlevel%
