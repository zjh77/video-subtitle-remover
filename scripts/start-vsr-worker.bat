@echo off
setlocal EnableExtensions

rem Start only the relay Worker. Start the local VSR API separately first.
rem The Worker configuration and Token remain in the ignored private directory.
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

set "VSR_RELAY_CONFIG_FILE=%WORKER_CONFIG%"
echo Starting relay Worker in this window ...
"%WORKER_PYTHON%" -m vsr_worker
set "EXIT_CODE=%ERRORLEVEL%"
popd
exit /b %EXIT_CODE%
