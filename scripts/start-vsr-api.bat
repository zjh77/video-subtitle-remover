@echo off
setlocal EnableExtensions

rem Start the loopback-only VSR API from this repository, using Conda env "vsr".
rem Pre-set VSR_API_HOST, VSR_API_PORT, or VSR_API_DATA_ROOT to override defaults.
set "PROJECT_ROOT=%~dp0.."
pushd "%PROJECT_ROOT%" >nul || (
  echo ERROR: Could not open the VSR project root.
  exit /b 1
)

where conda >nul 2>&1
if errorlevel 1 (
  echo ERROR: Conda is not available on PATH. Open an Anaconda/Miniconda prompt or add conda to PATH, then retry.
  popd
  exit /b 1
)

call conda activate vsr
if errorlevel 1 (
  echo ERROR: Could not activate the Conda environment named "vsr".
  popd
  exit /b 1
)

where python >nul 2>&1
if errorlevel 1 (
  echo ERROR: Python is unavailable after activating the "vsr" environment.
  popd
  exit /b 1
)

if not defined VSR_API_HOST set "VSR_API_HOST=127.0.0.1"
if not defined VSR_API_PORT set "VSR_API_PORT=8020"
if not defined VSR_API_DATA_ROOT set "VSR_API_DATA_ROOT=D:\short\vsr-data"

echo Starting VSR API on %VSR_API_HOST%:%VSR_API_PORT% ...
python -m vsr_api.run
set "EXIT_CODE=%ERRORLEVEL%"
popd
exit /b %EXIT_CODE%
