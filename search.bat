@echo off
rem ===========================================================
rem  PharmaCrawler - search the local library
rem  Keep this file ASCII-only (see gui.bat for the reason).
rem
rem  Usage examples:
rem    search.bat aspirin
rem    search.bat --mesh "Diabetes Mellitus, Type 2" --year 2023,2024
rem    search.bat metformin --scope abstract --has-doi --limit 50
rem    search.bat --source pubmed --journal "Nature" --export out.xlsx
rem ===========================================================
chcp 65001 >nul
setlocal
cd /d "%~dp0"

call :find_python
if not defined PYEXE (
  echo [ERROR] No usable Python found. Install Python 3.9+ and add it to PATH.
  pause
  exit /b 1
)

"%PYEXE%" -X utf8 pharma_crawler.py search %*
set "RC=%ERRORLEVEL%"
endlocal & exit /b %RC%

:find_python
set "PYEXE="
set "CAND=%USERPROFILE%\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe"
if exist "%CAND%" (
  "%CAND%" -c "import sys" >nul 2>nul && set "PYEXE=%CAND%"
)
if not defined PYEXE (
  where py >nul 2>nul && set "PYEXE=py"
)
if not defined PYEXE (
  for /f "delims=" %%i in ('where python 2^>nul') do (
    if not defined PYEXE (
      "%%i" -c "import sys" >nul 2>nul && set "PYEXE=%%i"
    )
  )
)
exit /b 0
