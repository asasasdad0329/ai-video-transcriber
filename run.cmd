@echo off
rem Same entry as run.ps1, for cmd, Git Bash and machines where .ps1 scripts are blocked.
rem Once the project Python exists it is started directly: Windows PowerShell in the
rem middle of a pipe can hand a native reader (python, jq) no data at all.
setlocal
set "VW_PYTHON="
for /d %%D in ("%~dp0.runtime\python\cpython-*-windows-x86_64-none") do if exist "%%D\python.exe" set "VW_PYTHON=%%D\python.exe"
if not defined VW_PYTHON goto first_run
"%VW_PYTHON%" -X utf8 -I -B -u "%~dp0system\bootstrap\launcher.py" %*
exit /b %ERRORLEVEL%
:first_run
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "%~dp0run.ps1" %*
exit /b %ERRORLEVEL%
