@echo off
echo ========================================
echo Сборка OfflineTranslator в EXE файл
echo ========================================
echo.

echo Устанавливаем PyInstaller...
pip install pyinstaller
echo.

echo Очищаем старые сборки...
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist
if exist *.spec del /q *.spec
echo.

echo Проверяем кэш моделей...
set BUNDLE_FLAGS=
if exist "cache\*" (
    echo Найдена папка cache\ — модели будут встроены в EXE.
    set BUNDLE_FLAGS=--add-data "cache;cache"
) else (
    if exist "%LOCALAPPDATA%\OfflineTranslator\cache\*" (
        echo Найдан кэш моделей в %%LOCALAPPDATA%%\OfflineTranslator\cache.
        echo Копируем модели в проект (для встраивания в EXE)...
        xcopy /E /I /Y "%LOCALAPPDATA%\OfflineTranslator\cache" "cache" >nul
        set BUNDLE_FLAGS=--add-data "cache;cache"
    ) else (
        echo ВНИМАНИЕ: кэш моделей не найден.
        echo EXE будет скачивать модели при ПЕРВОМ запуске (нужен интернет).
        echo Чтобы встроить модели в EXE, сначала запустите приложение один раз
        echo (python main.py), затем повторите сборку.
    )
)
echo.

echo Начинаем сборку...
echo (Это займет 2-5 минут)
pyinstaller --onefile ^
    --windowed ^
    --name "OfflineTranslator" ^
    --add-data "dictionary.json;." ^
    %BUNDLE_FLAGS% ^
    --hidden-import torch ^
    --hidden-import transformers ^
    --hidden-import customtkinter ^
    main.py

echo.
echo ========================================
if exist dist\OfflineTranslator.exe (
    echo ✓ СБОРКА УСПЕШНА!
    echo.
    echo EXE файл находится в папке: %CD%\dist\
    echo Размер файла: 
    dir dist\OfflineTranslator.exe | find "OfflineTranslator.exe"
    echo.
    echo Можешь скопировать OfflineTranslator.exe куда угодно!
) else (
    echo ✗ ОШИБКА СБОРКИ
    echo Проверь консоль выше
)
echo ========================================
pause