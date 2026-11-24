@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"

echo ==============================================
echo   ScreenDimmer Build Script
echo ==============================================

rem ---------- check python ----------
where python >nul 2>nul
if errorlevel 1 (
    echo [ERROR] python not found in PATH
    pause
    exit /b 1
)

rem ---------- ensure PyInstaller ----------
python -c "import PyInstaller" >nul 2>nul
if errorlevel 1 (
    echo [build] PyInstaller not installed, installing...
    python -m pip install pyinstaller
    if errorlevel 1 (
        echo [ERROR] failed to install PyInstaller
        pause
        exit /b 1
    )
)

rem ---------- detect python env prefix (conda compatible) ----------
for /f "delims=" %%i in ('python -c "import sys; print(sys.prefix)"') do set "PREFIX=%%i"
echo [build] Python env: %PREFIX%

rem ---------- collect runtime deps (conda layout: libffi / tcl / tk) ----------
set "ARGS=--onefile --noconsole --clean --name ScreenDimmer"

if exist "%PREFIX%\Library\bin\ffi.dll"    set ARGS=!ARGS! --add-binary "%PREFIX%\Library\bin\ffi.dll;."
if exist "%PREFIX%\Library\bin\tcl86t.dll" set ARGS=!ARGS! --add-binary "%PREFIX%\Library\bin\tcl86t.dll;."
if exist "%PREFIX%\Library\bin\tk86t.dll"  set ARGS=!ARGS! --add-binary "%PREFIX%\Library\bin\tk86t.dll;."
if exist "%PREFIX%\Library\lib\tcl8.6"     set ARGS=!ARGS! --add-data "%PREFIX%\Library\lib\tcl8.6;tcl\tcl8.6"
if exist "%PREFIX%\Library\lib\tk8.6"      set ARGS=!ARGS! --add-data "%PREFIX%\Library\lib\tk8.6;tcl\tk8.6"
if exist "%PREFIX%\Library\lib\tcl8"       set ARGS=!ARGS! --add-data "%PREFIX%\Library\lib\tcl8;tcl\tcl8"
if exist "%PREFIX%\Library\lib\dde1.4"     set ARGS=!ARGS! --add-data "%PREFIX%\Library\lib\dde1.4;tcl\dde1.4"
if exist "%PREFIX%\Library\lib\reg1.3"     set ARGS=!ARGS! --add-data "%PREFIX%\Library\lib\reg1.3;tcl\reg1.3"
if exist "%PREFIX%\Library\lib\itcl4.3.0"  set ARGS=!ARGS! --add-data "%PREFIX%\Library\lib\itcl4.3.0;tcl\itcl4.3.0"

rem ---------- check old exe not locked ----------
if exist "dist\ScreenDimmer.exe" (
    del "dist\ScreenDimmer.exe" >nul 2>nul
    if exist "dist\ScreenDimmer.exe" (
        echo [ERROR] dist\ScreenDimmer.exe is locked. Close the running ScreenDimmer first.
        pause
        exit /b 1
    )
)

rem ---------- build ----------
echo [build] building...
python -m PyInstaller %ARGS% dim_gui.py
if errorlevel 1 (
    echo [ERROR] build failed, see log above
    pause
    exit /b 1
)

rem ---------- cleanup ----------
if exist "build" rmdir /s /q build
if exist "ScreenDimmer.spec" del "ScreenDimmer.spec"

echo.
echo ==============================================
echo   Done: %~dp0dist\ScreenDimmer.exe
echo ==============================================
pause
