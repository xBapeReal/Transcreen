@echo off
rem Transcreen launcher. The first run installs everything that is missing:
rem   1. uv (official installer from astral.sh), 2. the Python dependencies,
rem   3. the offline en->tr model (~476 MB, skipped when use_local_mt is false).
rem Later runs only re-check (fast) and start the app without a console.
setlocal
cd /d "%~dp0"
set "UV_PROJECT_ENVIRONMENT=%LOCALAPPDATA%\uv-envs\transcreen"
set "PYTHONUTF8=1"
set "PYTHONPATH=%~dp0src"
set "DATA_DIR=%APPDATA%\Transcreen"

call :find_uv
if defined UV goto sync
echo uv bulunamadi, tek seferlik kuruluyor: https://docs.astral.sh/uv/
powershell -NoProfile -ExecutionPolicy ByPass -Command "irm https://astral.sh/uv/install.ps1 | iex"
call :find_uv
if defined UV goto sync
echo.
echo uv kurulamadi. Elle kurup Transcreen.bat'i yeniden calistir:
echo     https://docs.astral.sh/uv/getting-started/installation/
pause
exit /b 1

:sync
"%UV%" sync
if not errorlevel 1 goto model
echo Bagimliliklar kurulamadi. Internet baglantisini kontrol edip yeniden dene.
pause
exit /b 1

:model
rem Same test as the app: any model.bin under the model folder means installed.
dir /s /b "%DATA_DIR%\models\en_tr\model.bin" >nul 2>&1
if not errorlevel 1 goto run
findstr /r /c:"\"use_local_mt\": *false" "%DATA_DIR%\config.json" >nul 2>&1
if not errorlevel 1 goto run
echo.
echo Tek seferlik: cevrimdisi ceviri modeli indiriliyor, yaklasik 476 MB.
"%UV_PROJECT_ENVIRONMENT%\Scripts\python.exe" -m transcreen --setup-local
if not errorlevel 1 goto run
echo.
echo Model kurulamadi. Transcreen simdilik bulut cevirisiyle aciliyor;
echo model bir sonraki acilista yeniden denenecek.
timeout /t 8 >nul

:run
start "" "%UV_PROJECT_ENVIRONMENT%\Scripts\pythonw.exe" -m transcreen
endlocal
exit /b 0

:find_uv
rem The standalone installer's default location first, then PATH.
set "UV="
if exist "%USERPROFILE%\.local\bin\uv.exe" set "UV=%USERPROFILE%\.local\bin\uv.exe"
if defined UV exit /b 0
for /f "delims=" %%I in ('where uv 2^>nul') do if not defined UV set "UV=%%I"
exit /b 0
