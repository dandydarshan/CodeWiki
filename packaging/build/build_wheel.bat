@echo off
REM ============================================================
REM  CodeWiki - compiled Windows wheel build (source-hidden)
REM  Location: packaging\build\build_wheel.bat
REM  Run from a plain cmd window, from anywhere:
REM      packaging\build\build_wheel.bat
REM  Output: dist\nc_codewiki-<ver>-cp312-cp312-win_amd64.whl
REM ============================================================

setlocal enabledelayedexpansion

REM ---- Repo root = two levels up from this script -----------
for %%i in ("%~dp0..\..") do set "REPO=%%~fi"

REM ---- Python version to build for (override: set PYVER=3.13) -
if not defined PYVER set "PYVER=3.12"

REM ---- Locate MSVC via vswhere (override: set VCVARS=...) ----
if not defined VCVARS (
    set "VSWHERE=%ProgramFiles(x86)%\Microsoft Visual Studio\Installer\vswhere.exe"
)
if not defined VCVARS (
    if not exist "!VSWHERE!" (
        echo ERROR: vswhere.exe not found. Install Visual Studio Build Tools
        echo        with the "Desktop development with C++" workload,
        echo        or set VCVARS to the full path of vcvars64.bat.
        exit /b 1
    )
    for /f "usebackq delims=" %%i in (`"!VSWHERE!" -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath`) do set "VSINSTALL=%%i"
    if not defined VSINSTALL (
        echo ERROR: no Visual Studio install with the C++ tools was found.
        echo        Add the "Desktop development with C++" workload.
        exit /b 1
    )
    set "VCVARS=!VSINSTALL!\VC\Auxiliary\Build\vcvars64.bat"
)

echo Repo:   %REPO%
echo Python: %PYVER%
echo VCVARS: %VCVARS%

echo.
echo === [1/7] Loading MSVC compiler environment ===
call "%VCVARS%"
if errorlevel 1 ( echo ERROR: vcvars failed. & exit /b 1 )

echo.
echo === [2/7] Entering repo and activating buildenv ===
cd /d "%REPO%" || ( echo ERROR: repo not found: %REPO% & exit /b 1 )
if not exist "buildenv\Scripts\activate.bat" (
    echo buildenv not found - creating it with Python %PYVER% ...
    py -%PYVER% -m venv buildenv || ( echo ERROR: could not create buildenv. Is Python %PYVER% installed? & exit /b 1 )
    call buildenv\Scripts\activate.bat
    python -m pip install --upgrade pip
    python -m pip install cython build wheel setuptools
    python -m pip install . || ( echo ERROR: project install failed & exit /b 1 )
) else (
    call buildenv\Scripts\activate.bat
)

echo.
echo === [3/7] Sanity checks ===
python -c "import setuptools, Cython; print('build tools OK')" || ( echo ERROR: build tools missing in buildenv & exit /b 1 )
if not exist "setup.py" ( echo ERROR: setup.py not found in repo root & exit /b 1 )
if not exist "pyproject.toml" ( echo ERROR: pyproject.toml not found & exit /b 1 )

echo.
echo === [4/7] Cleaning previous build artifacts ===
if exist build        rmdir /s /q build
if exist build_cython rmdir /s /q build_cython
if exist _wheel       rmdir /s /q _wheel
if exist dist         rmdir /s /q dist
del /s /q codewiki\*.pyd >nul 2>&1
del /s /q codewiki\*.c   >nul 2>&1

echo.
echo === [5/7] Compiling modules with Cython + MSVC ===
set CODEWIKI_CYTHONIZE=1
set DISTUTILS_USE_SDK=1
set MSSdk=1
python setup.py build_ext --inplace || ( echo ERROR: compile failed - see output above & exit /b 1 )

echo.
echo === [6/7] Building the wheel ===
python -m build --wheel --no-isolation || ( echo ERROR: wheel build failed & exit /b 1 )

echo.
echo === [7/7] Stripping source (.py that has a compiled .pyd sibling) ===
python "%~dp0strip_wheel.py" "dist\*.whl" || ( echo ERROR: strip step failed & exit /b 1 )

echo.
echo === BUILD COMPLETE ===
echo Wheel: %REPO%\dist\
echo Expected readable source: the pydantic model files + templates\*.py only.
echo If a core logic module appears, add it to EXCLUDE in setup.py and re-run.
endlocal
