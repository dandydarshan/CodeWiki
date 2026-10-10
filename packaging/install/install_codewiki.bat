@echo off
REM ============================================================
REM  CodeWiki installer (Windows x86_64)
REM  Checks prerequisites, then installs nc-codewiki from PyPI
REM  with uv, or with pip into an installed Python 3.12-3.14 if
REM  you'd rather not use uv. Double-click it, or run from cmd:
REM      install_codewiki.bat
REM
REM  nc_codewiki-*.whl files placed next to this script are used
REM  too (e.g. a pre-release build); otherwise everything comes
REM  from PyPI.
REM ============================================================

setlocal enabledelayedexpansion
set "FAIL=0"
REM Wheels are published for these Python versions only.
set "PY_RANGE=>=3.12,<3.15"
set "HERE=%~dp0"
if "%HERE:~-1%"=="\" set "HERE=%HERE:~0,-1%"

echo.
echo ===========================================================
echo   CodeWiki installer (Windows)
echo ===========================================================
echo.

if /i not "%PROCESSOR_ARCHITECTURE%"=="AMD64" if /i not "%PROCESSOR_ARCHITEW6432%"=="AMD64" (
    echo ERROR: this installer is for 64-bit x86 Windows ^(found %PROCESSOR_ARCHITECTURE%^).
    pause
    exit /b 1
)

REM ---- 1. git present? --------------------------------------
echo [1/4] Checking for git ...
where git >nul 2>&1
if errorlevel 1 (
    echo    MISSING: git was not found on PATH.
    echo       CodeWiki needs git to analyze repositories.
    echo       Install it from https://git-scm.com/download/win , then re-run.
    set "FAIL=1"
) else (
    echo    OK: git found
)

REM ---- 2. Node.js / npm present? ----------------------------
echo [2/4] Checking for Node.js / npm ...
where npm >nul 2>&1
if errorlevel 1 (
    echo    MISSING: Node.js / npm was not found on PATH.
    echo       A dependency ^(PythonMonkey's pminit^) runs npm during install.
    echo       Install Node.js LTS from https://nodejs.org , then re-run.
    set "FAIL=1"
) else (
    echo    OK: npm found
)

if "%FAIL%"=="1" (
    echo.
    echo ===========================================================
    echo   Cannot continue - fix the MISSING items above and re-run.
    echo ===========================================================
    echo.
    pause
    exit /b 1
)

REM ---- 3. uv, or pip as a fallback ---------------------------
echo [3/4] Choosing how to install (uv or pip) ...
REM uv's installer puts uv here; make it visible to this window too.
set "PATH=%USERPROFILE%\.local\bin;%PATH%"
set "METHOD="
where uv >nul 2>&1 && set "METHOD=uv"
if not defined METHOD (
    echo    uv ^(Python package manager, https://docs.astral.sh/uv/^) is not installed.
    echo    It installs CodeWiki in its own environment and fetches a suitable Python.
    echo    Without it, CodeWiki is installed with pip instead.
    set "ANSWER="
    set /p "ANSWER=   Install uv now with its official installer? [y/N] "
    set "WANT="
    if /i "!ANSWER!"=="y" set "WANT=1"
    if /i "!ANSWER!"=="yes" set "WANT=1"
    if defined WANT (
        powershell -NoProfile -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
        where uv >nul 2>&1 && set "METHOD=uv"
        if not defined METHOD echo    uv install failed - falling back to pip.
    )
)

if not defined METHOD (
    REM pip needs a 64-bit Python version that has a published build.
    set "PYEXE="
    for %%V in (3.14 3.13 3.12) do (
        if not defined PYEXE (
            py -%%V -c "import sys; sys.exit(0 if sys.maxsize > 2**32 else 1)" >nul 2>&1 && set "PYEXE=py -%%V"
        )
        if not defined PYEXE (
            python -c "import sys; sys.exit(0 if '{0}.{1}'.format(*sys.version_info) == '%%V' and sys.maxsize > 2**32 else 1)" >nul 2>&1 && set "PYEXE=python"
        )
    )
    if not defined PYEXE (
        echo    MISSING: pip install needs 64-bit Python 3.12, 3.13 or 3.14, and none was found.
        echo       Install it from https://www.python.org/downloads/windows/
        echo       ^(64-bit installer, tick "Add python.exe to PATH"^), or install uv,
        echo       which downloads a suitable Python itself. Then re-run.
        pause
        exit /b 1
    )
    set "METHOD=pip"
)
if "%METHOD%"=="uv" (
    for /f "delims=" %%v in ('uv --version') do echo    OK: using %%v
) else (
    for /f "delims=" %%v in ('%PYEXE% --version') do echo    OK: using pip with %%v
)

REM ---- 4. install ---------------------------------------------
echo [4/4] Installing CodeWiki with %METHOD% ...
echo.
if "%METHOD%"=="uv" (
    uv tool install --upgrade --python "%PY_RANGE%" --find-links "%HERE%" nc-codewiki
    if errorlevel 1 (
        echo ERROR: install failed. See output above.
        pause
        exit /b 1
    )
    REM Put uv's tool directory on PATH in future terminals (no-op if already there).
    uv tool update-shell >nul 2>&1
    set "UPGRADE=uv tool upgrade nc-codewiki"
    set "UNINSTALL=uv tool uninstall nc-codewiki"
) else (
    %PYEXE% -m pip install --upgrade --find-links "%HERE%" nc-codewiki
    if errorlevel 1 (
        echo ERROR: install failed. See output above.
        pause
        exit /b 1
    )
    set "UPGRADE=%PYEXE% -m pip install --upgrade nc-codewiki"
    set "UNINSTALL=%PYEXE% -m pip uninstall nc-codewiki"
)

echo.
echo Verifying ...
if "%METHOD%"=="uv" (
    set "UVBIN="
    for /f "delims=" %%d in ('uv tool dir --bin') do set "UVBIN=%%d"
    "!UVBIN!\codewiki.exe" --version
) else (
    %PYEXE% -m codewiki --version
)
if errorlevel 1 (
    echo WARNING: 'codewiki --version' did not run cleanly - check the output above.
) else (
    echo    OK: codewiki works.
)

echo.
echo ===========================================================
echo   Done. Open a new terminal, then run:  codewiki --help
echo   Upgrade later:  %UPGRADE%
echo   Uninstall:      %UNINSTALL%
if "%METHOD%"=="pip" (
    where codewiki >nul 2>&1
    if errorlevel 1 (
        for /f "delims=" %%d in ('%PYEXE% -c "import sysconfig; print(sysconfig.get_path('scripts'))"') do (
            echo.
            echo   'codewiki' is not on PATH yet. Add this folder to your PATH:
            echo     %%d
        )
    )
)
echo ===========================================================
echo.
pause
endlocal
