@echo off
setlocal
echo ========================================
echo Сборка OfflineTranslator для Windows
echo ========================================
echo.

echo Устанавливаем зависимости сборки...
python -m pip install --upgrade pip
if errorlevel 1 exit /b 1
python -m pip install -r requirements.txt pyinstaller
if errorlevel 1 exit /b 1
echo.

echo Собираем приложение...
python build.py
if errorlevel 1 exit /b 1

echo.
echo Архивируем бандль...
powershell -NoProfile -ExecutionPolicy Bypass -Command "Compress-Archive -Path 'dist\OfflineTranslator.exe' -DestinationPath 'OfflineTranslator-Windows.zip' -Force"
if errorlevel 1 exit /b 1

echo.
echo ========================================
echo СБОРКА УСПЕШНА!
echo Архив: %CD%\OfflineTranslator-Windows.zip
echo ========================================
endlocal
