@echo off
rem ===========================================================
rem  PharmaCrawler GUI launcher
rem  NOTE: keep this file ASCII-only. cmd.exe reads .bat files
rem  using the system ANSI codepage, so non-ASCII text here
rem  would be garbled regardless of chcp.
rem ===========================================================
chcp 65001 >nul
setlocal enabledelayedexpansion
cd /d "%~dp0"
title PharmaCrawler

call :find_python
if not defined PYEXE (
  echo [ERROR] No usable Python found. Install Python 3.9+ and add it to PATH.
  pause
  exit /b 1
)

"%PYEXE%" -X utf8 -c "import tkinter" >nul 2>nul
if errorlevel 1 (
  echo [INFO] This Python has no tkinter - falling back to command line mode.
  echo        Examples:
  echo          crawl.bat --dataset label --search "openfda.brand_name:\"aspirin\""
  echo          crawl.bat --term "aspirin[Title/Abstract]"
  echo.
  "%PYEXE%" -X utf8 pharma_crawler.py --help
  pause
  exit /b 0
)

start "" "%PYEXE%" -X utf8 pharma_crawler.py gui
endlocal
exit /b 0

:find_python
set "PYEXE="
rem 1) bundled runtime used by the dev environment
set "CAND=%USERPROFILE%\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe"
if exist "%CAND%" (
  "%CAND%" -c "import sys" >nul 2>nul && set "PYEXE=%CAND%"
)
rem 2) the py launcher, which knows about every installed version
if not defined PYEXE (
  where py >nul 2>nul && set "PYEXE=py"
)
rem 3) plain python on PATH
if not defined PYEXE (
  for /f "delims=" %%i in ('where python 2^>nul') do (
    if not defined PYEXE (
      "%%i" -c "import sys" >nul 2>nul && set "PYEXE=%%i"
    )
  )
)
exit /b 0
