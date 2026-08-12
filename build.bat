@echo off
REM ============================================================
REM  Empaqueta Blip como un unico Blip.exe autonomo.
REM  Ejecuta este fichero (doble clic o "build.bat" en terminal)
REM  cada vez que cambies app.py y quieras regenerar el .exe.
REM ============================================================

cd /d "%~dp0"

echo.
echo [Blip] Cerrando instancias en ejecucion...
taskkill /IM Blip.exe /F >nul 2>&1

echo [Blip] Empaquetando con PyInstaller...
python -m PyInstaller --onefile --windowed --name Blip --noconfirm app.py
if errorlevel 1 (
  echo.
  echo [Blip] ERROR: fallo el empaquetado. Revisa el mensaje de arriba.
  pause
  exit /b 1
)

echo [Blip] Copiando Blip.exe a la raiz del proyecto...
copy /Y "dist\Blip.exe" "Blip.exe" >nul

echo.
echo [Blip] Listo. Blip.exe actualizado en:
echo        %~dp0Blip.exe
echo.
pause
