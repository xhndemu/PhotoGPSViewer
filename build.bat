@echo off
setlocal
cd /d "%~dp0"

echo ============================================
echo PhotoGPSViewer build script
echo ============================================
echo.

where python >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python was not found in PATH.
    echo Install Python 3.10 or newer, then enable Add Python to PATH.
    goto wait
)

echo [1/4] Installing PyInstaller...
python -m pip install --upgrade pyinstaller
if errorlevel 1 (
    echo ERROR: Failed to install PyInstaller.
    goto wait
)

echo.
echo [2/4] Cleaning previous output...
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist
if exist PhotoGPSViewer.zip del /q PhotoGPSViewer.zip

echo.
echo [3/4] Building application. This can take several minutes...
python -m PyInstaller PhotoGPSViewer.spec --noconfirm --clean
if errorlevel 1 (
    echo.
    echo ERROR: PyInstaller build failed. Read the messages above.
    goto wait
)

echo.
echo [4/4] Creating distributable ZIP...
powershell -NoProfile -Command "Compress-Archive -Path 'dist\PhotoGPSViewer' -DestinationPath 'PhotoGPSViewer.zip' -CompressionLevel Optimal"
if errorlevel 1 (
    echo WARNING: ZIP creation failed.
    echo The runnable application is still in dist\PhotoGPSViewer\
    goto wait
)

echo.
echo ============================================
echo Build complete.
echo Share this file: PhotoGPSViewer.zip
echo ============================================

:wait
echo.
echo Press any key to close this window...
pause >nul
