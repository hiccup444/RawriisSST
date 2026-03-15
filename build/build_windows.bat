@echo off
:: Always resolve paths relative to this .bat file, regardless of where it's run from
set "BUILDDIR=%~dp0"
set "PROJROOT=%BUILDDIR%.."

echo ============================================================
echo  RawriisSTT -- Windows .exe build
echo ============================================================
echo.

:: Install build dependencies (boto3 must be present so PyInstaller can bundle it)
python -m pip install --quiet pyinstaller pillow openvr boto3
if errorlevel 1 (
    echo ERROR: pip failed. Make sure Python is on your PATH.
    pause
    exit /b 1
)

:: Build — distpath and workpath are anchored to the project root
python -m PyInstaller "%BUILDDIR%windows.spec" ^
    --clean ^
    --noconfirm ^
    --distpath "%PROJROOT%\dist" ^
    --workpath "%BUILDDIR%work"

if errorlevel 1 (
    echo.
    echo BUILD FAILED. Check the output above for errors.
    pause
    exit /b 1
)

echo.
echo ============================================================
echo  Build complete!
echo  Output: dist\RawriisSTT\RawriisSTT.exe
echo ============================================================
pause
