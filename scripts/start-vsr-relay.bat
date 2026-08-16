@echo off
setlocal EnableExtensions

rem Start the loopback VSR API in a separate window, then run the relay Worker here.
rem The Worker configuration and its Token remain in the ignored private directory.
set "PROJECT_ROOT=%~dp0.."
set "WORKER_CONFIG=%~dp0..\private\vsr-worker.json"
set "WORKER_PYTHON=D:\conda_envs\vsr\python.exe"

pushd "%PROJECT_ROOT%" >nul || (
  echo ERROR: Could not open the VSR project root.
  exit /b 1
)

if not exist "%WORKER_CONFIG%" (
  echo ERROR: Worker configuration file is missing from the private directory.
  popd
  exit /b 1
)

if not exist "%WORKER_PYTHON%" (
  echo ERROR: Worker Python was not found. Check the VSR Conda environment installation.
  popd
  exit /b 1
)

call :check_api
if not errorlevel 1 (
  echo Local VSR API is already healthy.
  goto api_ready
)

echo Starting local VSR API in a separate window ...
start "VSR API" /D "%PROJECT_ROOT%" cmd.exe /d /c call "%~dp0start-vsr-api.bat"

set /a ATTEMPT=0
:wait_for_api
set /a ATTEMPT+=1
timeout /t 2 /nobreak >nul
call :check_api
if not errorlevel 1 goto api_ready
if %ATTEMPT% GEQ 30 goto api_failed
goto wait_for_api

:api_failed
echo ERROR: Local VSR API did not become healthy within 60 seconds.
echo Check the separate "VSR API" window for a non-sensitive error message.
popd
exit /b 1

:api_ready
echo Local VSR API is healthy. Starting relay Worker in this window ...
set "VSR_RELAY_CONFIG_FILE=%WORKER_CONFIG%"
"%WORKER_PYTHON%" -m vsr_worker
set "EXIT_CODE=%ERRORLEVEL%"
popd
exit /b %EXIT_CODE%

:check_api
powershell.exe -NoProfile -NonInteractive -Command "$ErrorActionPreference='Stop'; $health=Invoke-RestMethod 'http://127.0.0.1:8020/health'; if($health.status -ne 'ok'){exit 1}" >nul 2>&1
exit /b %ERRORLEVEL%
