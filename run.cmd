@echo off
rem Same entry as run.ps1, for cmd, Git Bash and machines where .ps1 scripts are blocked.
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "%~dp0run.ps1" %*
exit /b %ERRORLEVEL%
