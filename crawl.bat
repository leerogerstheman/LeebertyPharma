@echo off
rem ===========================================================
rem  PharmaCrawler - command line crawl
rem  Passes every argument straight through to pharma_crawler.py
rem  Keep this file ASCII-only (see gui.bat for the reason).
rem
rem  Usage examples:
rem    crawl.bat --dataset label --search "openfda.brand_name:\"aspirin\""
rem    crawl.bat --dataset enforcement --search "classification:\"Class I\""
rem    crawl.bat --term "metformin[Title/Abstract] AND 2020:2024[PDAT]"
rem    crawl.bat --dataset drugsfda --search "sponsor_name:\"Pfizer\"" ^
rem              --term "pfizer[Affiliation]"
rem    crawl.bat --dataset event --search "receivedate:[20230101+TO+20231231]" --dry-run
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

"%PYEXE%" -X utf8 pharma_crawler.py crawl %*
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" (
  echo.
  echo [exit code %RC%]
)
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
