@echo off
rem ===========================================================
rem  PharmaCrawler web interface launcher
rem
rem  Same backend as gui.bat - pharma_crawler.py is shared - but the
rem  interface is served by the standard library, so this one does NOT
rem  need tkinter. That matters on machines where the bundled Python was
rem  built without it: gui.bat falls back to command-line mode there,
rem  while the web interface still works.
rem
rem  NOTE: keep this file ASCII-only. cmd.exe reads .bat files using the
rem  system ANSI codepage, so non-ASCII text here would be garbled
rem  regardless of chcp. Chinese messages come from the Python side.
rem ===========================================================
chcp 65001 >nul
setlocal enabledelayedexpansion
cd /d "%~dp0"
title PharmaCrawler (web)

call :find_python
if not defined PYEXE (
  echo [ERROR] No usable Python found. Install Python 3.9+ and add it to PATH.
  pause
  exit /b 1
)

start "" "%PYEXE%" -X utf8 webui.py %*
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
