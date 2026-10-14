@echo off
setlocal EnableExtensions EnableDelayedExpansion

set "SCRIPT_DIR=%~dp0"
pushd "%SCRIPT_DIR%" >nul

if not exist "jira.env" (
  echo [error] Missing jira.env in %SCRIPT_DIR%
  popd >nul
  exit /b 1
)

call :read_env_var JIRA_URL
call :read_env_var JIRA_USERNAME
call :read_env_var JIRA_API_TOKEN
call :read_env_var JIRA_PASSWORD

if not defined JIRA_URL (
  echo [error] JIRA_URL is missing in jira.env
  popd >nul
  exit /b 1
)

if not defined JIRA_USERNAME (
  echo [error] JIRA_USERNAME is missing in jira.env
  popd >nul
  exit /b 1
)

if not defined JIRA_API_TOKEN if defined JIRA_PASSWORD set "JIRA_API_TOKEN=!JIRA_PASSWORD!"

if not defined JIRA_API_TOKEN (
  echo [error] Set JIRA_API_TOKEN in jira.env ^(or JIRA_PASSWORD for Jira Data Center password auth^)
  popd >nul
  exit /b 1
)

set "ACTION=%~1"
if "%ACTION%"=="" set "ACTION=start"

if /I "%ACTION%"=="start" goto start
if /I "%ACTION%"=="restart" goto restart
if /I "%ACTION%"=="stop" goto stop
if /I "%ACTION%"=="status" goto status
if /I "%ACTION%"=="logs" goto logs
if /I "%ACTION%"=="test" goto test
if /I "%ACTION%"=="help" goto help

echo [error] Unknown action: %ACTION%
goto help

:start
echo [info] Starting jira-mcp...
docker compose up -d --build jira-mcp
if errorlevel 1 goto fail
goto status

:restart
echo [info] Restarting jira-mcp...
docker compose up -d --build jira-mcp --force-recreate
if errorlevel 1 goto fail
goto status

:stop
echo [info] Stopping jira-mcp...
docker compose stop jira-mcp
if errorlevel 1 goto fail
goto end

:status
echo [info] jira-mcp status
docker compose ps jira-mcp
if errorlevel 1 goto fail
goto end

:logs
echo [info] jira-mcp logs
docker compose logs jira-mcp --tail 120
if errorlevel 1 goto fail
goto end

:test
echo [info] Testing Flask -> jira-mcp path
docker compose exec flask-app python -c "import requests; r=requests.get('http://jira-mcp:8000/sse', timeout=8); print('status:', r.status_code)"
if errorlevel 1 goto fail
goto end

:help
echo Usage: %~nx0 [start^|restart^|stop^|status^|logs^|test^|help]
echo.
echo   start   - build and start jira-mcp
echo   restart - force recreate jira-mcp container
echo   stop    - stop jira-mcp
echo   status  - show jira-mcp container state
echo   logs    - show recent jira-mcp logs
echo   test    - test MCP endpoint from flask-app container
goto end

:fail
echo [error] Command failed
popd >nul
exit /b 1

:end
popd >nul
exit /b 0

:read_env_var
set "%~1="
for /f "usebackq tokens=1* delims==" %%A in (`findstr /R /C:"^%~1=" "jira.env"`) do (
  set "%~1=%%B"
  goto :eof
)
goto :eof

