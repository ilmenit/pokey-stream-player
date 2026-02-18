@echo off
REM ============================================================================
REM POKEY Stream Player - Windows Build Script
REM ============================================================================
REM Builds standalone encode.exe using PyInstaller
REM
REM Requirements:
REM   - Python 3.8+ installed and in PATH
REM   - Internet connection (for pip install on first run)
REM
REM Usage:
REM   build.bat           - Build encode.exe
REM   build.bat dist      - Build AND create distribution zip
REM   build.bat clean     - Clean build directories
REM   build.bat check     - Check dependencies only
REM   build.bat install   - Install dependencies only
REM ============================================================================

setlocal enabledelayedexpansion

echo.
echo ============================================================
echo   POKEY Stream Player - Windows Build
echo ============================================================
echo.

REM Change to script directory
cd /d "%~dp0"

REM Parse arguments
if "%1"=="clean" goto :do_clean
if "%1"=="check" goto :do_check_only
if "%1"=="install" goto :do_install
if "%1"=="dist" goto :do_dist
if "%1"=="help" goto :do_help
if "%1"=="-help" goto :do_help
if "%1"=="/?" goto :do_help

REM Default: full build
goto :do_build

REM ============================================================================
:do_help
REM ============================================================================
echo Usage: build.bat [command]
echo.
echo Commands:
echo   (none)    Build encode.exe
echo   dist      Build AND create distribution zip
echo   clean     Clean build directories
echo   check     Check dependencies only
echo   install   Install Python dependencies
echo   help      Show this help
echo.
exit /b 0

REM ============================================================================
:do_clean
REM ============================================================================
echo Cleaning build directories...
if exist "build" rmdir /s /q "build"
if exist "dist" rmdir /s /q "dist"
if exist "release" rmdir /s /q "release"
for %%f in (pokey-stream-player-*.zip) do del "%%f" 2>nul
for /d /r %%d in (__pycache__) do @if exist "%%d" rmdir /s /q "%%d"
echo Done.
exit /b 0

REM ============================================================================
:do_install
REM ============================================================================
echo Installing dependencies...
python -m pip install --upgrade pip
if !errorlevel! neq 0 (
    echo [ERROR] pip upgrade failed
    exit /b 1
)
python -m pip install numpy scipy soundfile pyinstaller
if !errorlevel! neq 0 (
    echo [ERROR] Package installation failed
    exit /b 1
)
echo.
echo Dependencies installed.
exit /b 0

REM ============================================================================
:do_check_only
REM ============================================================================
call :check_all
if !errorlevel! neq 0 exit /b 1
exit /b 0

REM ============================================================================
:do_build
REM ============================================================================
call :check_all
if !errorlevel! neq 0 (
    echo.
    echo Please fix the issues above, or run: build.bat install
    exit /b 1
)

echo.
echo Installing/updating dependencies...
python -m pip install --quiet --upgrade numpy scipy soundfile pyinstaller
if !errorlevel! neq 0 (
    echo [ERROR] Failed to install dependencies
    exit /b 1
)

echo.
echo Building standalone executable...
echo.

python -m PyInstaller encode.spec --noconfirm --clean
if !errorlevel! neq 0 (
    echo.
    echo [ERROR] Build failed!
    exit /b 1
)

echo.
echo ============================================================
echo   BUILD SUCCESSFUL!
echo ============================================================
echo.

if exist "dist\encode.exe" (
    for %%f in ("dist\encode.exe") do (
        echo   Output: %%~ff
        echo   Size:   %%~zf bytes
    )
    echo.
    echo   To run:  dist\encode.exe song.mp3
    echo   Help:    dist\encode.exe -h
    echo.
    echo   To create a distribution zip: build.bat dist
)

echo.
exit /b 0

REM ============================================================================
:do_dist
REM ============================================================================
call :check_all
if !errorlevel! neq 0 (
    echo.
    echo Please fix the issues above before building.
    exit /b 1
)

echo.
echo Installing/updating dependencies...
python -m pip install --quiet --upgrade numpy scipy soundfile pyinstaller
if !errorlevel! neq 0 (
    echo [ERROR] Failed to install dependencies
    exit /b 1
)

echo.
echo Building standalone executable...
echo.

python -m PyInstaller encode.spec --noconfirm --clean
if !errorlevel! neq 0 (
    echo.
    echo [ERROR] Build failed!
    exit /b 1
)

if not exist "dist\encode.exe" (
    echo [ERROR] encode.exe not found
    exit /b 1
)

echo.
echo Creating distribution...
echo.

set "RELDIR=release\pokey-stream-player"
if exist "release" rmdir /s /q "release"
mkdir "%RELDIR%"

REM Copy executable
copy "dist\encode.exe" "%RELDIR%\" >nul
echo   [+] encode.exe

REM Copy docs
if exist "README.md" (
    copy "README.md" "%RELDIR%\" >nul
    echo   [+] README.md
)
if exist "LICENSE" (
    copy "LICENSE" "%RELDIR%\" >nul
    echo   [+] LICENSE
)

REM Copy MADS if available (optional — built-in assembler works without it)
if exist "bin\windows_x86_64\mads.exe" (
    copy "bin\windows_x86_64\mads.exe" "%RELDIR%\" >nul
    echo   [+] mads.exe ^(external assembler^)
) else (
    echo   [?] mads.exe not found — built-in assembler will be used
)

REM Create zip
set "ZIPNAME=pokey-stream-player-win64.zip"
if exist "%ZIPNAME%" del "%ZIPNAME%"
powershell -Command "Compress-Archive -Path '%RELDIR%\*' -DestinationPath '%ZIPNAME%'" 2>nul

echo.
echo ============================================================
echo   DISTRIBUTION READY
echo ============================================================
echo.
echo   Folder: %RELDIR%\
if exist "%ZIPNAME%" (
    for %%f in ("%ZIPNAME%") do echo   Zip:    %%~nxf (%%~zf bytes^)
)
echo.
echo   Contents:
dir /b "%RELDIR%"
echo.
echo   Usage:  encode.exe song.mp3
echo   Help:   encode.exe -h
echo.
echo   Optional: place mads.exe next to encode.exe for
echo   external MADS assembly (otherwise uses built-in).
echo.
exit /b 0


REM ============================================================================
REM SUBROUTINE: check_all
REM ============================================================================
:check_all
echo Checking Python...
python --version >nul 2>&1
if !errorlevel! neq 0 (
    echo   [ERROR] Python not found in PATH
    echo   Please install Python 3.8+ from https://python.org
    exit /b 1
)
for /f "tokens=2" %%v in ('python --version 2^>^&1') do set PYVER=%%v
echo   Python: %PYVER%

echo.
echo Checking required packages...
set MISSING=

call :check_pkg numpy
call :check_pkg scipy
call :check_pkg soundfile
call :check_pkg PyInstaller

echo.
echo Checking ASM templates...
if exist "asm\stream_player.asm" (
    echo   [OK] asm\ directory found
) else (
    echo   [X] asm\ directory missing!
    set "MISSING=!MISSING! ASM"
)

echo.
echo Checking MADS (optional)...
if exist "bin\windows_x86_64\mads.exe" (
    echo   [OK] bin\windows_x86_64\mads.exe — will be included in dist
) else (
    where mads.exe >nul 2>&1
    if !errorlevel! equ 0 (
        echo   [OK] mads.exe found in PATH
    ) else (
        echo   [?] mads.exe not found — built-in assembler will be used
        echo       To include MADS: place mads.exe in bin\windows_x86_64\
    )
)

echo.
echo Checking FFmpeg (optional)...
if exist "bin\windows_x86_64\ffmpeg.exe" (
    echo   [OK] ffmpeg.exe found — MOD/XM/S3M/IT import enabled
) else (
    where ffmpeg.exe >nul 2>&1
    if !errorlevel! equ 0 (
        echo   [OK] ffmpeg.exe found in PATH
    ) else (
        echo   [?] ffmpeg.exe not found — WAV/MP3/FLAC/OGG still work
        echo       Only needed for tracker formats ^(MOD, XM, S3M, IT^)
    )
)

echo.
if defined MISSING (
    echo ============================================================
    echo   Missing components:%MISSING%
    echo ============================================================
    echo.
    echo To install Python packages:
    echo   build.bat install
    echo.
    exit /b 1
)

echo ============================================================
echo   All checks passed!
echo ============================================================
exit /b 0


REM ============================================================================
REM SUBROUTINE: check_pkg
REM ============================================================================
:check_pkg
python -c "import %~1" >nul 2>&1
if !errorlevel! neq 0 (
    echo   [X] %~1 - MISSING
    set "MISSING=!MISSING! %~1"
) else (
    echo   [OK] %~1
)
exit /b 0
